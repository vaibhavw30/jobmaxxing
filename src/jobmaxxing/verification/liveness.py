"""HTTP liveness check for a job posting URL."""

from dataclasses import dataclass

import httpx

_HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")}
_TIMEOUT = 15.0


@dataclass
class Liveness:
    kind: str                 # 'alive' | 'dead' | 'transient'
    status: int | None = None
    error: str | None = None


def default_fetcher(url: str) -> int:
    """GET the URL (following redirects) and return the final HTTP status code. GET not HEAD —
    many ATS boards reject or mis-handle HEAD."""
    return httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True).status_code


def check_liveness(url, *, fetcher=default_fetcher) -> Liveness:
    """Classify a URL: 2xx/3xx-final -> alive; 404/410 -> dead; everything else (incl. a fetch
    exception) -> transient (retry later)."""
    try:
        status = fetcher(url)
    except Exception as exc:  # noqa: BLE001 - network/timeout is just a transient miss
        return Liveness("transient", None, type(exc).__name__)
    if 200 <= status < 400:
        return Liveness("alive", status)
    if status in (404, 410):
        return Liveness("dead", status)
    return Liveness("transient", status)
