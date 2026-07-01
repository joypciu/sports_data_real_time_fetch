import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import sofascore_client
import sofascore_db
import sofascore_tournaments
from dotenv import load_dotenv

script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(script_dir, ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SPORTS = {
    "football": {"statistics": True, "incidents": True, "lineups": False},
    "ice-hockey": {"statistics": False, "incidents": True, "lineups": True},
    "tennis": {"statistics": False, "incidents": False, "lineups": False},
    "baseball": {"statistics": False, "incidents": False, "lineups": True},
}

_SPORT_ALIASES: dict[str, str] = {
    "mlb": "baseball",
    "soccer": "football",
    "hockey": "ice-hockey",
}


def _resolve_sports_filter(sports: list[str] | None) -> dict[str, dict[str, bool]]:
    if not sports:
        return SPORTS
    selected: dict[str, dict[str, bool]] = {}
    for raw in sports:
        key = _SPORT_ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if key not in SPORTS:
            valid = ", ".join(sorted(SPORTS))
            raise ValueError(f"Unknown sport '{raw}'. Valid: {valid} (aliases: mlb, soccer, hockey)")
        selected[key] = SPORTS[key]
    return selected


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


def _process_events(
    sport: str,
    date_str: str,
    config: dict[str, bool],
    events: list[dict],
    *,
    session: sofascore_client.SofaScoreSession,
    seen_event_ids: set[str],
    counters: dict[str, int],
) -> None:
    for event in events:
        event_id = str(event.get("id"))
        if not event_id or event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)

        counters["seen"] += 1
        status_type = event.get("status", {}).get("type", "")

        sofascore_db.upsert_event(sport, date_str, event)
        counters["upserted"] += 1

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
                    counters["details"] += 1

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
                    counters["details"] += 1
                time.sleep(sofascore_client.detail_delay())


def run_ingest(days_back: int = 1, sports: list[str] | None = None) -> None:
    """Fetch and cache SofaScore data for today and up to days_back days."""
    sports_to_run = _resolve_sports_filter(sports)
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

    sofascore_tournaments.clear_tournament_cache()
    session = sofascore_client.begin_scraper_session()
    err_msg = ""

    try:
        category_plan: dict[str, list[dict]] = {}
        tournament_plan: dict[str, list[dict]] = {}
        for sport in sports_to_run:
            category_plan[sport] = sofascore_tournaments.resolve_category_scheduled_sources(
                sport
            )
            tournament_plan[sport] = sofascore_tournaments.resolve_tournaments_for_sport(
                sport,
                session=session,
            )
            if not tournament_plan[sport] and not category_plan[sport]:
                logger.error("No tournaments configured for sport=%s", sport)

        for date_str in dates_to_fetch:
            for sport, config in sports_to_run.items():
                tournaments = tournament_plan.get(sport) or []
                categories = category_plan.get(sport) or []
                if not tournaments and not categories:
                    logger.error("No tournaments configured for sport=%s", sport)
                    last_fetch_error = "no_tournaments_configured"
                    continue

                seen_event_ids: set[str] = set()
                sport_counters = {"seen": 0, "upserted": 0, "details": 0}
                batch_delay = sofascore_client.ingest_batch_delay()

                if categories:
                    logger.info(
                        "Fetching %s category scheduled-events for %s across %d categories",
                        sport,
                        date_str,
                        len(categories),
                    )
                    for category in categories:
                        category_id = int(category["id"])
                        category_name = category.get("name") or str(category_id)
                        schedule_attempts += 1

                        result = sofascore_client.get_with_meta(
                            f"/category/{category_id}/scheduled-events/{date_str}",
                            for_scraper=True,
                            session=session,
                        )

                        if result.ok:
                            events = result.data.get("events") or []
                            if events:
                                logger.info(
                                    "Got %d events for %s %s (category %s)",
                                    len(events),
                                    sport,
                                    date_str,
                                    category_name,
                                )
                                _process_events(
                                    sport,
                                    date_str,
                                    config,
                                    events,
                                    session=session,
                                    seen_event_ids=seen_event_ids,
                                    counters=sport_counters,
                                )
                            time.sleep(batch_delay)
                            continue

                        if result.status_code == 404:
                            continue

                        schedule_failures += 1
                        last_fetch_error = result.error or f"http_{result.status_code}"
                        logger.error(
                            "Category scheduled-events FAILED %s %s category=%s (%s): "
                            "error=%s status=%s (proxy=%s)",
                            sport,
                            date_str,
                            category_id,
                            category_name,
                            result.error,
                            result.status_code,
                            "yes" if proxy_used else "NO — set SOFASCORE_SCRAPER_PROXY",
                        )
                        time.sleep(batch_delay)

                if tournaments:
                    logger.info(
                        "Fetching %s scheduled-events for %s across %d tournaments",
                        sport,
                        date_str,
                        len(tournaments),
                    )

                for tournament in tournaments:
                    tournament_id = int(tournament["id"])
                    tournament_name = tournament.get("name") or str(tournament_id)
                    schedule_attempts += 1

                    result = sofascore_client.get_with_meta(
                        f"/unique-tournament/{tournament_id}/scheduled-events/{date_str}",
                        for_scraper=True,
                        session=session,
                    )

                    if result.ok:
                        events = result.data.get("events") or []
                        if events:
                            logger.info(
                                "Got %d events for %s %s (%s)",
                                len(events),
                                sport,
                                date_str,
                                tournament_name,
                            )
                            _process_events(
                                sport,
                                date_str,
                                config,
                                events,
                                session=session,
                                seen_event_ids=seen_event_ids,
                                counters=sport_counters,
                            )
                        time.sleep(batch_delay)
                        continue

                    if result.status_code == 404:
                        continue

                    schedule_failures += 1
                    last_fetch_error = result.error or f"http_{result.status_code}"
                    logger.error(
                        "Scheduled-events FAILED %s %s tournament=%s (%s): "
                        "error=%s status=%s (proxy=%s)",
                        sport,
                        date_str,
                        tournament_id,
                        tournament_name,
                        result.error,
                        result.status_code,
                        "yes" if proxy_used else "NO — set SOFASCORE_SCRAPER_PROXY",
                    )
                    time.sleep(batch_delay)

                total_events_seen += sport_counters["seen"]
                total_events_upserted += sport_counters["upserted"]
                total_details_fetched += sport_counters["details"]

                logger.info(
                    "Sport summary %s %s: seen=%d upserted=%d details=%d",
                    sport,
                    date_str,
                    sport_counters["seen"],
                    sport_counters["upserted"],
                    sport_counters["details"],
                )

        if schedule_attempts > 0 and schedule_failures == schedule_attempts:
            status = "failed"
            err_msg = (
                f"All {schedule_attempts} tournament scheduled-events requests failed "
                f"(last: {last_fetch_error}). "
                "Check SOFASCORE_SCRAPER_PROXY and tournaments.json."
            )
        elif schedule_failures > 0:
            status = "partial"
            err_msg = (
                f"{schedule_failures}/{schedule_attempts} tournament requests failed "
                f"(last: {last_fetch_error})"
            )
        elif total_events_seen == 0:
            status = "partial"
            err_msg = "HTTP OK but zero events returned for all tournaments/dates"
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
        reindexed = sofascore_db.reindex_utc_game_dates()
        if reindexed:
            logger.info("Reindexed %d event game_date values to UTC.", reindexed)
    except Exception as exc:
        logger.error("Failed to reindex UTC game dates: %s", exc)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SofaScore background ingest")
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Days back from today (0=today only, 1=today+yesterday)",
    )
    parser.add_argument(
        "--sport",
        action="append",
        dest="sports",
        metavar="SPORT",
        help=(
            "Ingest only this sport (repeatable). "
            "Values: football, tennis, ice-hockey, baseball. "
            "Aliases: soccer, hockey, mlb."
        ),
    )
    args = parser.parse_args()
    run_ingest(days_back=args.days, sports=args.sports)
