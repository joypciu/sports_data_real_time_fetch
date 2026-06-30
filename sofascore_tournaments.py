"""Load and resolve SofaScore unique-tournament IDs for ingest."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import sofascore_client

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tournaments.json")
_geo_cache: dict[str, list[dict[str, Any]]] = {}


def _load_config() -> dict[str, Any]:
    with open(_CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def clear_tournament_cache() -> None:
    """Reset per-run geo tournament cache."""
    _geo_cache.clear()


def _dedupe_tournaments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        tid = int(item["id"])
        if tid in seen:
            continue
        seen.add(tid)
        out.append({"id": tid, "name": item.get("name") or str(tid)})
    return out


def _extract_tournaments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if "uniqueTournaments" in payload:
        for t in payload.get("uniqueTournaments") or []:
            if t.get("id") is not None:
                out.append({"id": int(t["id"]), "name": t.get("name", "")})
    for group in payload.get("groups") or []:
        for t in group.get("uniqueTournaments") or []:
            if t.get("id") is not None:
                out.append({"id": int(t["id"]), "name": t.get("name", "")})
    return out


def _fetch_geo_tournaments(
    sport: str,
    country_codes: list[str],
    *,
    session: sofascore_client.SofaScoreSession | None,
) -> list[dict[str, Any]]:
    cache_key = f"{sport}:{'|'.join(country_codes)}"
    if cache_key in _geo_cache:
        return _geo_cache[cache_key]

    found: list[dict[str, Any]] = []
    for code in country_codes:
        result = sofascore_client.get_with_meta(
            f"/config/default-unique-tournaments/{code}/{sport}",
            for_scraper=True,
            session=session,
            retries=2,
        )
        if not result.ok:
            logger.warning(
                "Geo tournament list failed sport=%s country=%s error=%s status=%s",
                sport,
                code,
                result.error,
                result.status_code,
            )
            continue
        found.extend(_extract_tournaments(result.data))

    deduped = _dedupe_tournaments(found)
    _geo_cache[cache_key] = deduped
    logger.info(
        "Resolved %d geo default tournaments for %s (%d countries)",
        len(deduped),
        sport,
        len(country_codes),
    )
    return deduped


def resolve_category_scheduled_sources(sport: str) -> list[dict[str, Any]]:
    """Return category IDs that use /category/{id}/scheduled-events/{date}."""
    config = _load_config()
    return list((config.get("category_scheduled_events") or {}).get(sport) or [])


def resolve_tournaments_for_sport(
    sport: str,
    *,
    session: sofascore_client.SofaScoreSession | None = None,
) -> list[dict[str, Any]]:
    """Return deduped tournament list for a sport from tournaments.json."""
    config = _load_config()
    static = list((config.get("tournaments") or {}).get(sport) or [])
    merged = list(static)

    geo_codes = (config.get("geo_defaults") or {}).get(sport) or []
    if geo_codes:
        merged.extend(_fetch_geo_tournaments(sport, geo_codes, session=session))

    deduped = _dedupe_tournaments(merged)
    logger.info(
        "Tournament plan for %s: %d static, %d total after geo/category merge",
        sport,
        len(static),
        len(deduped),
    )
    return deduped
