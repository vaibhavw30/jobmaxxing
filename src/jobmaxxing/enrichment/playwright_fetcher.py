"""The real WorkdayFetcher: headless Chromium with per-host Cloudflare-clearance reuse.

Imported lazily by the worker so CI (no playwright installed) never loads it. All status
and challenge classification is delegated to enrichment.workday's pure helpers.
"""

import httpx

from .workday import (
    WorkdayBlocked, WorkdayNotFound, WorkdayTransient,
    _classify_status, _looks_like_challenge, workday_cxs_url, workday_host,
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
        try:
            self._browser = self._pw.chromium.launch(headless=headless)
        except Exception:
            self._pw.stop()  # don't leak the playwright process (e.g. `playwright install` not run)
            raise
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
        if host in self._contexts:
            return self._contexts[host]
        # Native bundled-Chromium UA (overriding to a stale Chrome/120 string would mismatch
        # the real TLS fingerprint and read as a bot signal). httpx Tier-0 keeps its own UA.
        ctx = self._browser.new_context(locale="en-US")
        page = ctx.new_page()
        try:
            page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=self._nav_timeout_ms)
            page.wait_for_timeout(self._settle_ms)  # let the CF JS challenge resolve
        except Exception as exc:  # noqa: BLE001 - warmup failed: close the orphan ctx, classify transient
            ctx.close()
            raise WorkdayTransient(f"cf-warmup: {exc}") from exc
        finally:
            page.close()
        self._contexts[host] = ctx
        return ctx

    def fetch_via_context(self, host: str, cxs_url: str) -> dict:
        ctx = self._cleared_context(host)
        r = ctx.request.get(cxs_url, headers={"Accept": "application/json"})
        _classify_status(r.status)
        return r.json()

    def fetch_via_render(self, job_url: str) -> dict:
        ctx = self._cleared_context(workday_host(job_url))
        # Match THIS job's exact cxs endpoint (sans query), not any /job/ cxs call: a stale
        # posting can redirect to the careers home, whose SPA fires a cxs call for a featured
        # job — capturing that would silently write a foreign description.
        target_cxs = workday_cxs_url(job_url)
        page = ctx.new_page()
        captured: dict = {}

        def on_response(resp):
            # Capture THIS job's exact cxs endpoint (not any /job/ cxs — a stale page can fire
            # a cxs call for a featured job, whose payload would be a wrong description).
            if target_cxs and resp.url.split("?", 1)[0] == target_cxs:
                captured["status"] = resp.status  # the SPA's own call may be 403/404 even when the page loads
                if resp.status == 200:
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
        if "status" in captured:
            # The SPA fired this job's cxs but not a parseable 200. Classify by its real status:
            # 403/429/503 -> blocked (the API itself is gated; transient, not "gone"),
            # 404/410 -> the posting is gone (permanent). _classify_status raises accordingly.
            _classify_status(captured["status"])
            raise WorkdayNotFound("cxs 200 but unparseable payload")  # 200-without-body: treat as gone
        # No job cxs fired at all -> a Cloudflare interstitial (retryable) or a genuinely dead page.
        if _looks_like_challenge(title):
            raise WorkdayBlocked("render blocked by cloudflare challenge")
        raise WorkdayNotFound("no cxs job call from rendered page")

    def close(self):
        # Defensive: a failure closing one resource must not leak the others (called from
        # the worker's `finally`). Always reach browser.close() and pw.stop().
        try:
            self._http.close()
        finally:
            try:
                self._browser.close()
            finally:
                self._pw.stop()
