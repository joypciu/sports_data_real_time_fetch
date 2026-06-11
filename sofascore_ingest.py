import time
import logging
import uuid
from datetime import datetime, timedelta, timezone
import sofascore_client
import sofascore_db

import os
from dotenv import load_dotenv

# Use absolute path to guarantee the cron job finds the .env file
script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(script_dir, '.env'))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SPORTS = {
    "football": {"statistics": True, "incidents": True, "lineups": False},
    "ice-hockey": {"statistics": False, "incidents": True, "lineups": True},
    "tennis": {"statistics": False, "incidents": False, "lineups": False},
}


def run_ingest(days_back: int = 1) -> None:
    """
    Fetch and cache Sofascore data for today and up to days_back days.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    
    dates_to_fetch = []
    for i in range(days_back + 1):
        dt = started_at - timedelta(days=i)
        dates_to_fetch.append(dt.strftime("%Y-%m-%d"))

    total_events_seen = 0
    total_events_upserted = 0
    total_details_fetched = 0

    sofascore_db.init_db()

    with sofascore_db.get_connection() as conn:
        conn.execute("""
            INSERT INTO sofascore_ingest_log (run_id, started_at, status)
            VALUES (?, ?, 'running')
        """, (run_id, started_at.isoformat()))

    try:
        for date_str in dates_to_fetch:
            for sport, config in SPORTS.items():
                logger.info(f"Fetching {sport} scheduled-events for {date_str}")
                schedule = sofascore_client.get(f"/sport/{sport}/scheduled-events/{date_str}", for_scraper=True)
                events = schedule.get("events", [])
                
                for event in events:
                    total_events_seen += 1
                    event_id = str(event.get("id"))
                    status_type = event.get("status", {}).get("type", "")
                    
                    # Upsert base event
                    sofascore_db.upsert_event(sport, date_str, event)
                    total_events_upserted += 1

                    # Only fetch details if the match is finished (or we need them)
                    if status_type == "finished":
                        # Football / Hockey details
                        for detail_key, is_required in config.items():
                            if is_required:
                                detail_payload = sofascore_db.lookup_details(event_id, detail_key)
                                if detail_payload is None:
                                    logger.info(f"Fetching {sport} {detail_key} for event {event_id}")
                                    resp = sofascore_client.get(f"/event/{event_id}/{detail_key}", for_scraper=True)
                                    if resp:
                                        sofascore_db.upsert_details(event_id, detail_key, resp)
                                        total_details_fetched += 1
                                    time.sleep(0.5) # Anti-rate limit

                        # Tennis fallback if score is incomplete
                        if sport == "tennis":
                            home_score = event.get("homeScore", {})
                            if not home_score.get("current"):
                                event_payload = sofascore_db.lookup_details(event_id, "event")
                                if event_payload is None:
                                    logger.info(f"Fetching tennis fallback event detail for {event_id}")
                                    resp = sofascore_client.get(f"/event/{event_id}", for_scraper=True)
                                    if resp:
                                        # Also upsert this as a detail so we don't fetch it again
                                        sofascore_db.upsert_details(event_id, "event", resp)
                                        # Update the main event record with the better data
                                        better_event = resp.get("event", {})
                                        if better_event:
                                            sofascore_db.upsert_event(sport, date_str, better_event)
                                        total_details_fetched += 1
                                    time.sleep(0.5)
        
        status = "ok"
        err_msg = ""
    except Exception as exc:
        logger.error(f"Ingest failed: {exc}")
        status = "failed"
        err_msg = str(exc)

    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    with sofascore_db.get_connection() as conn:
        conn.execute("""
            UPDATE sofascore_ingest_log
            SET finished_at = ?, events_seen = ?, events_upserted = ?, details_fetched = ?,
                status = ?, error_message = ?, duration_sec = ?
            WHERE run_id = ?
        """, (finished_at.isoformat(), total_events_seen, total_events_upserted, total_details_fetched,
              status, err_msg, duration, run_id))
    
    logger.info(f"Ingest {status}. Seen: {total_events_seen}, Upserted: {total_events_upserted}, Details: {total_details_fetched}, Duration: {duration:.1f}s")

    # Clean up data older than 30 days to keep the DB small
    try:
        sofascore_db.prune_old_events(days=30)
        logger.info("Pruned events older than 30 days.")
    except Exception as e:
        logger.error(f"Failed to prune old events: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1, help="Number of previous days to fetch (0 = today only, 1 = today + yesterday)")
    args = parser.parse_args()
    run_ingest(days_back=args.days)
