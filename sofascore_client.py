import os
import random
import time
from typing import Any

try:
    from curl_cffi import requests as _cffi_requests  # type: ignore[import]

    _CURL_AVAILABLE = True
except ImportError:
    import httpx as _httpx  # type: ignore[import]

    _CURL_AVAILABLE = False

_BASE = "https://www.sofascore.com/api/v1"
_TIMEOUT = float(os.environ.get("SOFASCORE_READ_TIMEOUT", "15.0"))

_IMPERSONATE_PROFILES = [
    "chrome",
    "chrome110",
    "chrome120",
    "chrome124",
    "edge101",
]

_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
}


def get_proxy(for_scraper: bool) -> str | None:
    """Resolve the proxy based on the caller context."""
    if for_scraper:
        return (
            os.getenv("SOFASCORE_SCRAPER_PROXY")
            or os.getenv("SOFASCORE_PROXY")
            or None
        )
    return os.getenv("SOFASCORE_PROXY") or None


def get(path: str, for_scraper: bool = False, retries: int = 3) -> dict[str, Any]:
    """Best-effort GET with Cloudflare bypass; returns {} on any failure."""
    url = f"{_BASE}{path}" if path.startswith("/") else path
    _proxy = get_proxy(for_scraper)
    
    _proxies = {"http": _proxy, "https": _proxy} if _proxy else None
    
    if _CURL_AVAILABLE:
        for attempt in range(retries):
            try:
                kwargs: dict[str, Any] = {
                    "headers": _HEADERS,
                    "timeout": _TIMEOUT,
                    "impersonate": random.choice(_IMPERSONATE_PROFILES),
                }
                if _proxy:
                    kwargs["proxies"] = _proxies
                resp = _cffi_requests.get(url, **kwargs)
                if resp.status_code == 429:
                    import logging
                    logging.getLogger(__name__).warning("curl_cffi get 429 rate limit (attempt %d/%d) for %s", attempt + 1, retries, url)
                    time.sleep(5)
                    continue
                if resp.status_code != 200:
                    import logging
                    logging.getLogger(__name__).warning("curl_cffi get failed with %d (attempt %d/%d) for %s: %s", resp.status_code, attempt + 1, retries, url, resp.text[:200] if hasattr(resp, 'text') else str(resp.content)[:200])
                    continue
                return resp.json()
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("curl_cffi get failed (attempt %d/%d): %s", attempt + 1, retries, exc)
                time.sleep(1)
        return {}
    else:
        for attempt in range(retries):
            try:
                resp = _httpx.get(
                    url,
                    headers={**_HEADERS, "User-Agent": "Mozilla/5.0"},
                    timeout=_TIMEOUT,
                    follow_redirects=True,
                    proxy=_proxy,
                )
                if resp.status_code == 429:
                    import logging
                    logging.getLogger(__name__).warning("httpx get 429 rate limit (attempt %d/%d) for %s", attempt + 1, retries, url)
                    time.sleep(5)
                    continue
                if resp.status_code != 200:
                    import logging
                    logging.getLogger(__name__).warning("httpx get failed with %d (attempt %d/%d) for %s: %s", resp.status_code, attempt + 1, retries, url, resp.text[:200])
                    continue
                return resp.json()
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("httpx get failed (attempt %d/%d): %s", attempt + 1, retries, exc)
                time.sleep(1)
        return {}
