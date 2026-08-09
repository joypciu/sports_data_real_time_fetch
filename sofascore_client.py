"""
HTTP client for SofaScore API with Cloudflare bypass.

Ingest (for_scraper=True) reuses a warmed curl_cffi Session so cookies and TLS
fingerprint stay consistent across an entire run. Live settlement may use a
shorter-lived session or direct GET.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any

try:
    from curl_cffi import requests as _cffi_requests  # type: ignore[import]

    _CURL_AVAILABLE = True
except ImportError:
    _CURL_AVAILABLE = False

import httpx as _httpx  # type: ignore[import]

logger = logging.getLogger(__name__)

_BASE = "https://www.sofascore.com/api/v1"
_SITE = "https://www.sofascore.com/"
_CONNECT_TIMEOUT = float(os.environ.get("SOFASCORE_CONNECT_TIMEOUT", "5.0"))
_READ_TIMEOUT = float(os.environ.get("SOFASCORE_READ_TIMEOUT", "20.0"))
_SETTLEMENT_READ_TIMEOUT = float(os.environ.get("SOFASCORE_SETTLEMENT_READ_TIMEOUT", "4.0"))
_INGEST_BATCH_DELAY = float(os.environ.get("SOFASCORE_INGEST_BATCH_DELAY", "2.0"))
_DETAIL_DELAY = float(os.environ.get("SOFASCORE_DETAIL_DELAY", "0.8"))

_IMPERSONATE_PROFILES = [
    p.strip()
    for p in os.environ.get(
        "SOFASCORE_IMPERSONATE_LIST",
        "chrome131,chrome124,chrome120,chrome110",
    ).split(",")
    if p.strip()
]

_BASE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}


def _parse_json_response(resp: Any) -> dict[str, Any] | None:
    """Parse JSON body; return None if not valid JSON."""
    try:
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _is_cloudflare_challenge(resp: Any, data: dict[str, Any] | None) -> bool:
    """
    True only for SofaScore/Cloudflare rejection — NOT tennis 'Challenger' tours.
    """
    if data and _response_has_payload(data):
        return False

    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        return resp.status_code in (403, 503)

    # Exact API error shape: {"error": {"code": 403, "reason": "challenge"}}
    if data and isinstance(data.get("error"), dict):
        err = data["error"]
        if err.get("code") == 403 and str(err.get("reason", "")).lower() == "challenge":
            return True

    if resp.status_code in (403, 503):
        lowered = text.lower()
        if '"reason": "challenge"' in lowered or '"reason":"challenge"' in lowered:
            return True
        if lowered.startswith("{") and "challenge" in lowered and "error" in lowered:
            return True
        return resp.status_code == 403

    return False


def _response_has_payload(data: dict[str, Any]) -> bool:
    """True when the JSON is usable API data (not an error envelope)."""
    if data.get("error"):
        return False
    for key in ("events", "statistics", "incidents", "lineups", "event"):
        val = data.get(key)
        if val is None:
            continue
        if isinstance(val, list) and len(val) > 0:
            return True
        if isinstance(val, dict) and val:
            return True
    return False


def _api_headers(*, xhr: bool = True) -> dict[str, str]:
    headers = dict(_BASE_HEADERS)
    if xhr:
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
    return headers


_SPORT_PATHS = {
    "tennis": "tennis",
    "soccer": "football",
    "football": "football",
    "hockey": "ice-hockey",
    "ice-hockey": "ice-hockey",
    "basketball": "basketball",
    "nba": "basketball",
    "wnba": "basketball",
    "baseball": "baseball",
    "mlb": "baseball",
}

# One scraper session per ingest run (set by sofascore_ingest).
_scraper_session: SofaScoreSession | None = None


@dataclass
class FetchResult:
    ok: bool
    status_code: int | None
    elapsed_sec: float
    data: dict[str, Any]
    error: str | None = None


class SofaScoreSession:
    """Reusable curl_cffi session with SofaScore cookie warmup."""

    def __init__(self, proxy: str | None, impersonate: str | None = None) -> None:
        self.proxy = proxy
        self._impersonate = impersonate or _IMPERSONATE_PROFILES[0]
        self._profile_index = _IMPERSONATE_PROFILES.index(self._impersonate) if self._impersonate in _IMPERSONATE_PROFILES else 0
        self._session: Any = None
        self._warmed = False

    def _build_session(self) -> Any:
        session = _cffi_requests.Session(impersonate=self._impersonate)
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        return session

    def _ensure_session(self) -> None:
        if self._session is None:
            self._session = self._build_session()

    def rotate_profile(self) -> None:
        """New browser fingerprint after a Cloudflare block."""
        self.close()
        self._profile_index = (self._profile_index + 1) % len(_IMPERSONATE_PROFILES)
        self._impersonate = _IMPERSONATE_PROFILES[self._profile_index]
        logger.info("Rotated SofaScore impersonate profile to %s", self._impersonate)

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None
        self._warmed = False

    def warmup(self, *, force: bool = False) -> bool:
        """Visit the site homepage first to pick up Cloudflare cookies."""
        if not _CURL_AVAILABLE:
            return False
        if self._warmed and not force:
            return True

        self._ensure_session()
        try:
            resp = self._session.get(
                _SITE,
                headers=_api_headers(xhr=False),
                timeout=_READ_TIMEOUT,
            )
            if resp.status_code not in (200, 304):
                logger.warning("SofaScore warmup returned %s", resp.status_code)
            time.sleep(1.0)
            self._warmed = True
            return True
        except Exception as exc:
            logger.warning("SofaScore warmup failed: %s", exc)
            return False

    def get(
        self,
        url: str,
        *,
        retries: int = 3,
        read_timeout: float | None = None,
    ) -> FetchResult:
        if not _CURL_AVAILABLE:
            return FetchResult(False, None, 0.0, {}, "curl_cffi_not_installed")

        if not self._warmed:
            self.warmup()

        timeout = read_timeout if read_timeout is not None else _READ_TIMEOUT
        started = time.monotonic()
        last_error: str | None = None
        last_status: int | None = None

        for attempt in range(retries):
            self._ensure_session()
            try:
                resp = self._session.get(
                    url, headers=_api_headers(), timeout=timeout
                )
                elapsed = time.monotonic() - started
                last_status = resp.status_code
                data = _parse_json_response(resp)

                if resp.status_code == 429:
                    last_error = "rate_limit_429"
                    logger.warning(
                        "SofaScore 429 (attempt %d/%d) %s",
                        attempt + 1,
                        retries,
                        url,
                    )
                    time.sleep(8 + attempt * 4)
                    continue

                if resp.status_code == 404:
                    return FetchResult(False, 404, elapsed, {}, "not_found")

                if data and _response_has_payload(data):
                    return FetchResult(True, resp.status_code, elapsed, data)

                if _is_cloudflare_challenge(resp, data):
                    last_error = "cloudflare_403"
                    body = (getattr(resp, "text", None) or "")[:200]
                    logger.warning(
                        "SofaScore Cloudflare block (attempt %d/%d) %s: %s",
                        attempt + 1,
                        retries,
                        url,
                        body,
                    )
                    self.rotate_profile()
                    self.warmup(force=True)
                    time.sleep(10 + attempt * 5)
                    continue

                if resp.status_code != 200:
                    last_error = f"http_{resp.status_code}"
                    logger.warning(
                        "SofaScore HTTP %d (attempt %d/%d) %s",
                        resp.status_code,
                        attempt + 1,
                        retries,
                        url,
                    )
                    time.sleep(2 + attempt)
                    continue

                if data is not None:
                    return FetchResult(True, 200, elapsed, data)

                return FetchResult(False, 200, elapsed, {}, "invalid_json")

            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "SofaScore request error (attempt %d/%d) %s: %s",
                    attempt + 1,
                    retries,
                    url,
                    exc,
                )
                if "429" in str(exc) or "CONNECT tunnel failed" in str(exc):
                    time.sleep(10 + attempt * 5)
                    self.rotate_profile()
                    self.warmup(force=True)
                else:
                    time.sleep(2 + attempt)

        return FetchResult(
            ok=False,
            status_code=last_status,
            elapsed_sec=time.monotonic() - started,
            data={},
            error=last_error or "exhausted_retries",
        )


def get_proxy(for_scraper: bool) -> str | None:
    if for_scraper:
        return (
            os.getenv("SOFASCORE_SCRAPER_PROXY")
            or os.getenv("SOFASCORE_PROXY")
            or None
        )
    return os.getenv("SOFASCORE_PROXY") or None


def ingest_batch_delay() -> float:
    return _INGEST_BATCH_DELAY


def detail_delay() -> float:
    return _DETAIL_DELAY


def begin_scraper_session(*, probe: bool = True) -> SofaScoreSession:
    """Start a shared session for one ingest run. Call close_scraper_session() when done."""
    global _scraper_session
    close_scraper_session()
    proxy = get_proxy(for_scraper=True)
    _scraper_session = SofaScoreSession(proxy)
    if proxy:
        logger.info("SofaScore scraper using proxy (host hidden)")
    else:
        logger.info("SofaScore scraper using direct connection (no proxy)")
    _scraper_session.warmup()

    if probe and proxy:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        probe_result = _scraper_session.get(
            f"{_BASE}/unique-tournament/17/scheduled-events/{today}",
            retries=1,
        )
        if not probe_result.ok and probe_result.error == "cloudflare_403":
            allow_fallback = os.getenv("SOFASCORE_PROXY_FALLBACK_DIRECT", "true").lower() in (
                "1",
                "true",
                "yes",
            )
            if allow_fallback:
                logger.warning(
                    "Scraper proxy is blocked by Cloudflare — retrying ingest "
                    "without proxy (set SOFASCORE_PROXY_FALLBACK_DIRECT=false to disable)"
                )
                _scraper_session.close()
                _scraper_session = SofaScoreSession(None)
                _scraper_session.warmup()
            else:
                logger.error(
                    "Scraper proxy blocked and SOFASCORE_PROXY_FALLBACK_DIRECT=false — "
                    "use a residential proxy or clear SOFASCORE_SCRAPER_PROXY locally"
                )

    return _scraper_session


def close_scraper_session() -> None:
    global _scraper_session
    if _scraper_session is not None:
        _scraper_session.close()
        _scraper_session = None


def get_with_meta(
    path: str,
    *,
    for_scraper: bool = False,
    for_settlement: bool = False,
    retries: int | None = None,
    session: SofaScoreSession | None = None,
) -> FetchResult:
    url = f"{_BASE}{path}" if path.startswith("/") else path
    attempt_count = retries if retries is not None else (1 if for_settlement else 3)

    if for_scraper and session is None:
        session = _scraper_session

    if _CURL_AVAILABLE and (for_scraper or session is not None or for_settlement):
        use_proxy = for_scraper or for_settlement
        active = session or SofaScoreSession(get_proxy(use_proxy))
        own_session = session is None
        read_timeout = _SETTLEMENT_READ_TIMEOUT if for_settlement else _READ_TIMEOUT
        try:
            if own_session:
                active.warmup()
            return active.get(url, retries=attempt_count, read_timeout=read_timeout)
        finally:
            if own_session:
                active.close()

    return _httpx_get_with_meta(
        url,
        for_scraper=for_scraper,
        retries=attempt_count,
        for_settlement=for_settlement,
    )


def _httpx_get_with_meta(
    url: str,
    *,
    for_scraper: bool,
    retries: int,
    for_settlement: bool = False,
) -> FetchResult:
    proxy = get_proxy(for_scraper)
    read_timeout = _SETTLEMENT_READ_TIMEOUT if for_settlement else _READ_TIMEOUT
    timeout = (_CONNECT_TIMEOUT, read_timeout)
    started = time.monotonic()
    last_error: str | None = None
    last_status: int | None = None

    for attempt in range(retries):
        try:
            resp = _httpx.get(
                url,
                headers={**_api_headers(), "User-Agent": "Mozilla/5.0"},
                timeout=timeout,
                follow_redirects=True,
                proxy=proxy,
            )
            last_status = resp.status_code
            data = _parse_json_response(resp)
            if data and _response_has_payload(data):
                return FetchResult(True, resp.status_code, time.monotonic() - started, data)
            if _is_cloudflare_challenge(resp, data):
                last_error = "cloudflare_403"
                time.sleep(10)
                continue
            if resp.status_code == 429:
                last_error = "rate_limit_429"
                time.sleep(8)
                continue
            if resp.status_code == 404:
                return FetchResult(False, 404, time.monotonic() - started, {}, "not_found")
            if resp.status_code != 200:
                last_error = f"http_{resp.status_code}"
                time.sleep(2)
                continue
            return FetchResult(True, 200, time.monotonic() - started, resp.json())
        except Exception as exc:
            last_error = str(exc)
            time.sleep(2)

    return FetchResult(
        ok=False,
        status_code=last_status,
        elapsed_sec=time.monotonic() - started,
        data={},
        error=last_error or "exhausted_retries",
    )


def get(path: str, for_scraper: bool = False, retries: int = 3) -> dict[str, Any]:
    return get_with_meta(path, for_scraper=for_scraper, retries=retries).data


def test_connectivity(
    sport: str,
    date: str,
    *,
    for_scraper: bool = False,
) -> dict[str, Any]:
    api_sport = _SPORT_PATHS.get(sport, sport)
    session: SofaScoreSession | None = None
    try:
        if for_scraper:
            session = begin_scraper_session()
        result = get_with_meta(
            f"/sport/{api_sport}/scheduled-events/{date}",
            for_scraper=for_scraper,
            retries=2,
            session=session,
        )
        events = result.data.get("events") or []
        return {
            "ok": result.ok and len(events) > 0,
            "http_ok": result.ok,
            "events": len(events),
            "elapsed_sec": round(result.elapsed_sec, 2),
            "status_code": result.status_code,
            "error": result.error,
            "proxy_configured": get_proxy(for_scraper) is not None,
            "impersonate": session._impersonate if session else _IMPERSONATE_PROFILES[0],
        }
    finally:
        if for_scraper:
            close_scraper_session()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="SofaScore client smoke test")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--scraper", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--sport", default="tennis")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()

    if args.test:
        for_scraper = args.scraper or not args.live
        print(json.dumps(test_connectivity(args.sport, args.date, for_scraper=for_scraper), indent=2))
