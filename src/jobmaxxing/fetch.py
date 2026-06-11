import httpx

DEFAULT_TIMEOUT = 30.0


def fetch_json(url: str, *, timeout: float = DEFAULT_TIMEOUT):
    """GET a URL and return parsed JSON. Raises on HTTP error (caller isolates per-source)."""
    resp = httpx.get(url, timeout=timeout, headers={"User-Agent": "jobmaxxing/0.1"})
    resp.raise_for_status()
    return resp.json()
