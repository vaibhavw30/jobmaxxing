"""The real WorkdayFetcher: headless Chromium with per-host Cloudflare-clearance reuse.

Imported lazily by the worker so CI (no playwright installed) never loads it. All status
and challenge classification is delegated to enrichment.workday's pure helpers.
"""

import httpx

from .workday import (
    WorkdayBlocked, WorkdayNotFound, WorkdayTransient,
    _classify_status, _looks_like_challenge, workday_host,
)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}


class PlaywrightFetcher:
    """One browser + a per-host Cloudflare-cleared context cache. NOT thread-safe (Playwright
    sync objects belong to their creating thread); the worker gives each pool thread its own
    instance and shards jobs by tenant so a tenant's clearance is established once and reused."""

    def __init__(self, *, headless: bool = True, settle_ms: int = 5000, nav_timeout_ms: int = 45000):
        from playwright.sync_api import sync_playwright  # lazy
        self._settle_ms, self._nav_timeout_ms = settle_ms, nav_timeout_ms
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._contexts: dict[str, object] = {}
        self._http = httpx.Client(headers=_HEADERS, timeout=20.0, follow_redirects=True)

    def fetch_plain(self, cxs_url: str) -> dict:
        try:
            r = self._http.get(cxs_url)
        except httpx.HTTPError as exc:
            raise WorkdayTransient(f"plain: {exc}") from exc
        _classify_status(r.status_code)
        return r.json()

    def _cleared_context(self, host: str):
        if host not in self._contexts:
            ctx = self._browser.new_context(user_agent=_UA, locale="en-US")
            page = ctx.new_page()
            try:
                page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
                page.wait_for_timeout(self._settle_ms)  # let the CF JS challenge resolve
            finally:
                page.close()
            self._contexts[host] = ctx
        return self._contexts[host]

    def fetch_via_context(self, host: str, cxs_url: str) -> dict:
        ctx = self._cleared_context(host)
        r = ctx.request.get(cxs_url, headers={"Accept": "application/json"})
        _classify_status(r.status)
        return r.json()

    def fetch_via_render(self, job_url: str) -> dict:
        ctx = self._cleared_context(workday_host(job_url))
        page = ctx.new_page()
        captured: dict = {}

        def on_response(resp):
            if "/wday/cxs/" in resp.url and "/job/" in resp.url and resp.status == 200:
                try:
                    captured["payload"] = resp.json()
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", on_response)
        title = ""
        try:
            page.goto(job_url, wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
            page.wait_for_timeout(self._settle_ms)
            title = page.title() or ""
        except Exception as exc:  # noqa: BLE001 - navigation failure
            raise WorkdayTransient(f"render: {exc}") from exc
        finally:
            page.close()
        if "payload" in captured:
            return captured["payload"]
        if _looks_like_challenge(title):
            raise WorkdayBlocked("render blocked by cloudflare challenge")
        raise WorkdayNotFound("no cxs job payload from rendered page")

    def close(self):
        self._http.close()
        self._browser.close()
        self._pw.stop()
