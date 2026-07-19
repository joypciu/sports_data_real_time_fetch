"""
Shared SofaScore live lookup for settlement when sofascore_db has no match.

Uses the same tournament/category endpoints as ingest (sport-level
scheduled-events is blocked). Respects a time budget so stats_api can
fall through to ESPN / other sources quickly.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import sofascore_client
import sofascore_db
import sofascore_tournaments

_LOOKUP_BUDGET_SEC = float(os.environ.get("SOFASCORE_SETTLEMENT_LOOKUP_BUDGET", "10.0"))

_BASEBALL_TOURNAMENT_ID = 11205

_FINISHED_DESCRIPTIONS = frozenset({"ended", "finished", "aet", "aot", "ap"})
_CANCELED_STATUSES = frozenset({"canceled", "cancelled"})


def event_is_finished(event: dict[str, Any]) -> bool:
    status_obj = event.get("status") or {}
    status_type = str(status_obj.get("type") or "").lower()
    description = str(status_obj.get("description") or "").lower()
    return status_type == "finished" or description in _FINISHED_DESCRIPTIONS


def event_is_canceled(event: dict[str, Any]) -> bool:
    """True when the event was canceled before play (no contest → void)."""
    status_obj = event.get("status") or {}
    status_type = str(status_obj.get("type") or "").lower()
    description = str(status_obj.get("description") or "").lower()
    return status_type in _CANCELED_STATUSES or description in _CANCELED_STATUSES


def needs_status_refresh(event: dict[str, Any]) -> bool:
    """True when cached event status/scores may be stale and /event/{id} should be rechecked."""
    if event_is_finished(event) or event_is_canceled(event):
        return False

    status_type = str((event.get("status") or {}).get("type") or "").lower()
    if status_type in {"inprogress", "live"}:
        return True

    ts = event.get("startTimestamp")
    if ts is not None:
        try:
            start = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            if datetime.now(timezone.utc) >= start + timedelta(minutes=105):
                return True
        except (TypeError, ValueError, OSError):
            pass

    return status_type not in {"notstarted", "postponed", "canceled", "cancelled"}


def refresh_event(event_id: str) -> dict[str, Any] | None:
    result = sofascore_client.get_with_meta(f"/event/{event_id}", for_settlement=True)
    if not result.ok or not result.data:
        return None
    payload = result.data
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    if not event.get("id"):
        return None
    return event


def refresh_db_event_if_stale(
    sport: str,
    game_date: str,
    event: dict[str, Any],
    *,
    allow_live: bool,
) -> tuple[dict[str, Any], str, bool]:
    """Refresh a cached event from SofaScore when status looks stale."""
    if not allow_live or not needs_status_refresh(event):
        return event, "sofascore_db", False

    refreshed = refresh_event(str(event["id"]))
    if refreshed is None:
        return event, "sofascore_db", False

    stored_date = sofascore_db.utc_game_date_from_event(refreshed, game_date)
    sofascore_db.upsert_event(sport, stored_date, refreshed)
    return refreshed, "sofascore", True


def schedule_dates(
    game_date: str,
    team_hint: str | None,
    opponent_hint: str | None,
) -> list[str]:
    """Bet date first; widen to ±1 day when both sides are known."""
    if team_hint and opponent_hint:
        dates = [game_date]
        for candidate in sofascore_db.date_candidates(game_date):
            if candidate != game_date:
                dates.append(candidate)
        return dates
    return sofascore_db.date_candidates(game_date)


def fetch_event_detail(
    event_id: str,
    detail_key: str,
    *,
    allow_live: bool = True,
    force_live: bool = False,
) -> dict[str, Any] | None:
    if not force_live:
        cached = sofascore_db.lookup_details(event_id, detail_key)
        if cached:
            return cached
    if not allow_live:
        return None
    result = sofascore_client.get_with_meta(
        f"/event/{event_id}/{detail_key}",
        for_settlement=True,
    )
    if result.ok and result.data:
        sofascore_db.upsert_details(event_id, detail_key, result.data)
        return result.data
    return None


def _over_budget(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _best_event(
    events: list[dict[str, Any]],
    team_hint: str | None,
    opponent_hint: str | None,
) -> tuple[dict[str, Any] | None, float]:
    best: dict[str, Any] | None = None
    best_score = -1.0
    for ev in events:
        home_name = (ev.get("homeTeam") or {}).get("name", "")
        away_name = (ev.get("awayTeam") or {}).get("name", "")
        score = sofascore_db.event_match_score(
            team_hint, opponent_hint, home_name, away_name
        )
        if score > best_score:
            best_score = score
            best = ev
    if best and best_score >= 0.55:
        return best, best_score
    return None, best_score


def _fetch_events(path: str) -> list[dict[str, Any]]:
    result = sofascore_client.get_with_meta(path, for_settlement=True)
    if not result.ok:
        return []
    return result.data.get("events") or []


def _static_tournament_sources(sport: str) -> list[tuple[str, int]]:
    config = sofascore_tournaments._load_config()
    static = (config.get("tournaments") or {}).get(sport) or []
    return [("tournament", int(item["id"])) for item in static]


def _category_sources(sport: str) -> list[tuple[str, int]]:
    return [
        ("category", int(item["id"]))
        for item in sofascore_tournaments.resolve_category_scheduled_sources(sport)
    ]


def _iter_lookup_sources(sport: str) -> list[tuple[str, int]]:
    if sport == "baseball":
        return [("tournament", _BASEBALL_TOURNAMENT_ID)]
    if sport == "tennis":
        return _category_sources(sport)
    return _static_tournament_sources(sport)


def find_live_event(
    sport: str,
    game_date: str,
    team_hint: str | None,
    opponent_hint: str | None,
    *,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    """
    Search SofaScore live scheduled-events via tournament/category endpoints.
    Returns the raw event dict or None.
    """
    if deadline is None:
        deadline = time.monotonic() + _LOOKUP_BUDGET_SEC

    best: dict[str, Any] | None = None
    sources = _iter_lookup_sources(sport)

    for candidate_date in schedule_dates(game_date, team_hint, opponent_hint):
        if _over_budget(deadline):
            break

        for source_type, source_id in sources:
            if _over_budget(deadline):
                break

            if source_type == "category":
                path = f"/category/{source_id}/scheduled-events/{candidate_date}"
            else:
                path = f"/unique-tournament/{source_id}/scheduled-events/{candidate_date}"

            events = _fetch_events(path)
            if not events:
                continue

            match, score = _best_event(events, team_hint, opponent_hint)
            if match is not None:
                return match

    return None
