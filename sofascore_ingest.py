import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import sofascore_client
import sofascore_db
from dotenv import load_dotenv

script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(script_dir, ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SPORTS = {
    "football": {"statistics": True, "incidents": True, "lineups": False},
    "ice-hockey": {"statistics": False, "incidents": True, "lineups": True},
    "tennis": {"statistics": False, "incidents": False, "lineups": False},
}


def _fetch_detail(event_id: str, detail_key: str, path_suffix: str) -> bool:
    result = sofascore_client.get_with_meta(path_suffix, for_scraper=True)
    if result.ok:
        sofascore_db.upsert_details(event_id, detail_key, result.data)
        time.sleep(sofascore_client.detail_delay())
        return True
    logger.warning(
        "Detail fetch failed event=%s type=%s error=%s status=%s",
        event_id,
        detail_key,
        result.error,
        result.status_code,
    )
    return False


def run_ingest(days_back: int = 1) -> None:
    """Fetch and cache SofaScore data for today and up to days_back days."""
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    proxy_used = 1 if sofascore_client.get_proxy(for_scraper=True) else 0

    dates_to_fetch = [
        (started_at - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days_back + 1)
    ]

    total_events_seen = 0
    total_events_upserted = 0
    total_details_fetched = 0
    schedule_attempts = 0
    schedule_failures = 0
    last_fetch_error = ""

    sofascore_db.init_db()

    with sofascore_db.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO sofascore_ingest_log (run_id, started_at, status, proxy_used)
            VALUES (?, ?, 'running', ?)
            """,
            (run_id, started_at.isoformat(), proxy_used),
        )

    session = sofascore_client.begin_scraper_session()
    err_msg = ""

    try:
        for date_str in dates_to_fetch:
            for sport, config in SPORTS.items():
                logger.info("Fetching %s scheduled-events for %s", sport, date_str)
                schedule_attempts += 1

                result = sofascore_client.get_with_meta(
                    f"/sport/{sport}/scheduled-events/{date_str}",
                    for_scraper=True,
                    session=session,
                )

                if not result.ok:
                    schedule_failures += 1
                    last_fetch_error = result.error or f"http_{result.status_code}"
                    logger.error(
                        "Scheduled-events FAILED %s %s: error=%s status=%s "
                        "(proxy=%s)",
                        sport,
                        date_str,
                        result.error,
                        result.status_code,
                        "yes" if proxy_used else "NO — set SOFASCORE_SCRAPER_PROXY",
                    )
                    time.sleep(sofascore_client.ingest_batch_delay())
                    continue

                events = result.data.get("events") or []
                logger.info("Got %d events for %s %s", len(events), sport, date_str)

                for event in events:
                    total_events_seen += 1
                    event_id = str(event.get("id"))
                    status_type = event.get("status", {}).get("type", "")

                    sofascore_db.upsert_event(sport, date_str, event)
                    total_events_upserted += 1

                    if status_type != "finished":
                        continue

                    for detail_key, is_required in config.items():
                        if not is_required:
                            continue
                        if sofascore_db.lookup_details(event_id, detail_key) is None:
                            logger.info("Fetching %s %s for event %s", sport, detail_key, event_id)
                            if _fetch_detail(
                                event_id,
                                detail_key,
                                f"/event/{event_id}/{detail_key}",
                            ):
                                total_details_fetched += 1

                    if sport == "tennis":
                        home_score = event.get("homeScore", {})
                        if not home_score.get("current") and sofascore_db.lookup_details(
                            event_id, "event"
                        ) is None:
                            logger.info("Fetching tennis event detail for %s", event_id)
                            detail_result = sofascore_client.get_with_meta(
                                f"/event/{event_id}",
                                for_scraper=True,
                                session=session,
                            )
                            if detail_result.ok:
                                sofascore_db.upsert_details(event_id, "event", detail_result.data)
                                better_event = detail_result.data.get("event", {})
                                if better_event:
                                    sofascore_db.upsert_event(sport, date_str, better_event)
                                total_details_fetched += 1
                            time.sleep(sofascore_client.detail_delay())

                time.sleep(sofascore_client.ingest_batch_delay())

        if schedule_attempts > 0 and schedule_failures == schedule_attempts:
            status = "failed"
            err_msg = (
                f"All {schedule_attempts} scheduled-events requests blocked "
                f"(last: {last_fetch_error}). "
                "Use a residential SOFASCORE_SCRAPER_PROXY on VPS."
            )
        elif schedule_failures > 0:
            status = "partial"
            err_msg = (
                f"{schedule_failures}/{schedule_attempts} scheduled-events failed "
                f"(last: {last_fetch_error})"
            )
        elif total_events_seen == 0:
            status = "partial"
            err_msg = "HTTP OK but zero events returned for all sport/date pairs"
        else:
            status = "ok"

    except Exception as exc:
        logger.error("Ingest failed: %s", exc)
        status = "failed"
        err_msg = str(exc)
    finally:
        sofascore_client.close_scraper_session()

    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    with sofascore_db.get_connection() as conn:
        conn.execute(
            """
            UPDATE sofascore_ingest_log
            SET finished_at = ?, events_seen = ?, events_upserted = ?, details_fetched = ?,
                status = ?, error_message = ?, duration_sec = ?, proxy_used = ?
            WHERE run_id = ?
            """,
            (
                finished_at.isoformat(),
                total_events_seen,
                total_events_upserted,
                total_details_fetched,
                status,
                err_msg,
                duration,
                proxy_used,
                run_id,
            ),
        )

    logger.info(
        "Ingest %s. Seen: %d, Upserted: %d, Details: %d, Duration: %.1fs%s",
        status,
        total_events_seen,
        total_events_upserted,
        total_details_fetched,
        duration,
        f" — {err_msg}" if err_msg else "",
    )

    try:
        sofascore_db.prune_old_events(days=30)
        logger.info("Pruned events older than 30 days.")
    except Exception as exc:
        logger.error("Failed to prune old events: %s", exc)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SofaScore background ingest")
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Days back from today (0=today only, 1=today+yesterday)",
    )
    args = parser.parse_args()
    run_ingest(days_back=args.days)
