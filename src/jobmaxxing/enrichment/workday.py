"""Workday JD enrichment — pure logic + tiered worker (browser code lives in playwright_fetcher).

Run LOCALLY via `python -m jobmaxxing.enrich_workday` (needs the `headless` extra).
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Protocol

import psycopg

from ..config import load_settings
from .enrich import Outcome, _apply_outcomes

logger = logging.getLogger(__name__)

# https://{tenant}.{wd}.myworkdayjobs.com/[xx-XX/]{site}/job/{rest}
_WORKDAY_RE = re.compile(
    r"https://(?P<tenant>[^.]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?"            # optional locale prefix, stripped
    r"(?P<site>[^/]+)/job/(?P<rest>.+)$"
)

# https://{wd}.myworkdaysite.com/[xx-XX/]recruiting/{tenant}/{site}/job/{rest}
# A second, functionally-equivalent Workday public domain: same cxs API, tenant lives in the
# path instead of the hostname. Verified live (2026-07-08): the derived cxs URL for real
# myworkdaysite.com postings (Magna, Snap, Microchip Technology) returns the identical
# Cloudflare-gate error shape a normal myworkdayjobs.com tenant returns from an uncleared
# context -- i.e. the SAME endpoint, reachable via the SAME tiered fetch below.
_WORKDAY_SITE_RE = re.compile(
    r"https://(?P<wd>wd\d+)\.myworkdaysite\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?"             # optional locale prefix, stripped
    r"recruiting/(?P<tenant>[^/]+)/(?P<site>[^/]+)/job/(?P<rest>.+)$"
)


def _match_workday(url: str) -> dict | None:
    """Try both recognized Workday URL shapes; return {tenant, wd, site, rest} from whichever
    matches, or None. Both shapes resolve to the identical downstream identity/cxs URL."""
    m = _WORKDAY_RE.match(url)
    if m:
        return m.groupdict()
    m = _WORKDAY_SITE_RE.match(url)
    return m.groupdict() if m else None


def workday_host(url: str) -> str | None:
    g = _match_workday(url)
    return f"{g['tenant']}.{g['wd']}.myworkdayjobs.com" if g else None


def workday_cxs_url(url: str) -> str | None:
    """Translate a Workday job URL (either recognized public-domain shape) to its cxs JSON
    endpoint, or None if unrecognized."""
    g = _match_workday(url)
    if not g:
        return None
    return (f"https://{g['tenant']}.{g['wd']}.myworkdayjobs.com/wday/cxs/"
            f"{g['tenant']}/{g['site']}/job/{g['rest']}")


def parse_workday(payload: dict) -> str | None:
    """Extract the (HTML) job description from a cxs payload, or None if absent."""
    jd = (payload or {}).get("jobPostingInfo", {}).get("jobDescription")
    return jd or None


class WorkdayBlocked(Exception):
    """Cloudflare/anti-bot blocked this fetch (403/429/503/challenge). Escalate a tier;
    if blocked at every tier, classify transient (a later run/tier may succeed)."""


class WorkdayNotFound(Exception):
    """Posting gone (404/410, or the rendered careers app fired no job cxs call). Permanent."""


class WorkdayTransient(Exception):
    """Timeout, connection error, or browser crash. Retry next run until the cap."""


def _classify_status(status: int):
    if status == 200:
        return None
    if status in (403, 429, 503):
        raise WorkdayBlocked(f"status {status}")
    if status in (404, 410):
        raise WorkdayNotFound(f"status {status}")
    raise WorkdayTransient(f"status {status}")


# Cloudflare interstitial titles ("Just a moment...", "Attention Required!", etc.).
_CHALLENGE_MARKERS = ("just a moment", "attention required", "checking your browser", "cloudflare")


def _looks_like_challenge(page_title: str) -> bool:
    t = (page_title or "").lower()
    return any(marker in t for marker in _CHALLENGE_MARKERS)


class WorkdayFetcher(Protocol):
    def fetch_plain(self, cxs_url: str) -> dict: ...           # Tier 0 (no browser)
    def fetch_via_context(self, host: str, cxs_url: str) -> dict: ...  # Tier 1
    def fetch_via_render(self, job_url: str) -> dict: ...      # Tier 2


def _outcome_from_payload(job_id, payload) -> Outcome:
    desc = parse_workday(payload)
    if not desc:
        return Outcome(job_id, "permanent", None, "no description in workday payload")
    return Outcome(job_id, "enriched", desc, None)


def fetch_workday_one(job_id, url: str, fetcher: WorkdayFetcher) -> Outcome:
    """Plain -> headless-context -> headless-render, classifying as it escalates.
    Pure w.r.t. the DB and the browser; all errors isolate into an Outcome."""
    cxs = workday_cxs_url(url)
    if cxs is None:
        return Outcome(job_id, "permanent", None, f"unrecognized workday url: {url}")
    host = workday_host(url)
    for tier in (
        lambda: fetcher.fetch_plain(cxs),
        lambda: fetcher.fetch_via_context(host, cxs),
        lambda: fetcher.fetch_via_render(url),
    ):
        try:
            return _outcome_from_payload(job_id, tier())
        except WorkdayNotFound as exc:
            return Outcome(job_id, "permanent", None, str(exc))
        except WorkdayTransient as exc:
            return Outcome(job_id, "transient", None, str(exc))
        except WorkdayBlocked:
            continue
        except Exception as exc:  # noqa: BLE001 - a buggy/crashing fetcher must never crash the shard
            return Outcome(job_id, "transient", None, f"{type(exc).__name__}: {exc}"[:500])
    return Outcome(job_id, "transient", None, "blocked at all tiers (cloudflare unsolved)")


def _default_fetcher_factory():
    from .playwright_fetcher import PlaywrightFetcher  # lazy: CI never imports playwright
    return PlaywrightFetcher()


def enrich_workday(conn, *, max_jobs=300, max_workers=3, cap=3, fetcher_factory=_default_fetcher_factory):
    """Local worker: enrich description-less Workday rows via the tiered headless fetch.

    Jobs are sharded by tenant host so each shard runs on one thread-local fetcher and reuses
    that tenant's Cloudflare clearance. Returns {enriched, permanent_failed, transient_failed,
    candidates}."""
    rows = conn.execute(
        "select id, url from jobs "
        "where coalesce(description, '') = '' "
        "and route_method is distinct from 'manual' "
        "and enrich_attempts < %s "
        "and url ~* 'myworkdayjobs\\.com|myworkdaysite\\.com' "
        "order by scraped_at desc "
        "limit %s",
        (cap, max_jobs),
    ).fetchall()
    if not rows:
        return {"enriched": 0, "permanent_failed": 0, "transient_failed": 0, "candidates": 0}

    shards: dict[str, list] = {}
    for job_id, url in rows:
        # workday_host always matches after the SQL `url ~* myworkdayjobs` filter; "" is a
        # defensive fallback that groups any hypothetical non-match into one shard.
        shards.setdefault(workday_host(url) or "", []).append((job_id, url))

    def run_shard(jobs):
        fetcher = fetcher_factory()
        try:
            return [fetch_workday_one(jid, url, fetcher) for jid, url in jobs]
        finally:
            close = getattr(fetcher, "close", None)
            if close:
                close()

    outcomes: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run_shard, jobs) for jobs in shards.values()]
        for future in as_completed(futures):
            outcomes.extend(future.result())

    counts = _apply_outcomes(conn, outcomes, cap=cap)
    counts["candidates"] = len(rows)
    logger.info("enrich_workday summary: %s", counts)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        print(f"workday enriched: {enrich_workday(conn)}")
