"""URL verification stage. Run LOCALLY: python -m jobmaxxing.verify_url

For the in-window triage backlog, confirm each posting URL resolves; when dead, promote a working
alternative (existing alt_url, else a confidently-matched search result); else mark it dead. Mirrors
recovery.recover_new's candidate-query + attempt-cap + batched-write shape.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

from ..config import load_settings
from ..normalize import in_window_term_labels, off_window_sql
from .find import find_alternative_url
from .liveness import check_liveness, default_fetcher

logger = logging.getLogger(__name__)

DEFAULT_CAP = 3
DEFAULT_MAX_JOBS = 200
DEFAULT_REVERIFY_DAYS = 14
DEFAULT_MAX_WORKERS = 5


@dataclass
class _Outcome:
    job_id: object
    kind: str                              # 'alive' | 'dead' | 'transient'
    new_url: str | None = None             # set when a working alternative is promoted
    new_alt_urls: list = field(default_factory=list)
    error: str | None = None


def _fold_alts(new_primary, old_url, alt_urls):
    """Old primary + existing alts, order-preserving dedup, excluding the new primary."""
    seen = [old_url, *(alt_urls or [])]
    return [u for u in dict.fromkeys(seen) if u != new_primary]


def _verify_one(job_id, url, alt_urls, company, title, *, liveness_fetcher, find_alt) -> _Outcome:
    live = check_liveness(url, fetcher=liveness_fetcher)
    if live.kind == "alive":
        return _Outcome(job_id, "alive")
    if live.kind == "transient":
        return _Outcome(job_id, "transient", error=str(live.status or live.error))
    # dead: try existing alts, then a confidently-matched search result; promote only if it resolves.
    for alt in alt_urls:
        if check_liveness(alt, fetcher=liveness_fetcher).kind == "alive":
            return _Outcome(job_id, "alive", new_url=alt, new_alt_urls=_fold_alts(alt, url, alt_urls))
    found = find_alt(company, title, url)
    if found and check_liveness(found, fetcher=liveness_fetcher).kind == "alive":
        return _Outcome(job_id, "alive", new_url=found, new_alt_urls=_fold_alts(found, url, alt_urls))
    return _Outcome(job_id, "dead", error=f"dead:{live.status}")


def verify_urls(conn: psycopg.Connection, *, now=None, cap=DEFAULT_CAP, max_jobs=DEFAULT_MAX_JOBS,
                reverify_days=DEFAULT_REVERIFY_DAYS, max_workers=DEFAULT_MAX_WORKERS,
                liveness_fetcher=default_fetcher, find_alt=find_alternative_url) -> dict:
    now = now or datetime.now(timezone.utc)
    labels = in_window_term_labels(now.date())
    cutoff = now - timedelta(days=reverify_days)
    rows = conn.execute(
        f"select id, url, alt_urls, company, title from jobs "
        f"where resume_type is not null and status in ('new', 'routed') "
        f"and not ({off_window_sql(labels)}) "
        f"and verify_attempts < %s and (verified_at is null or verified_at < %s) "
        f"order by verified_at asc nulls first, scraped_at desc limit %s",
        (cap, cutoff, max_jobs),
    ).fetchall()
    if not rows:
        return {"alive": 0, "promoted": 0, "dead": 0, "transient": 0, "candidates": 0}

    outcomes: list[_Outcome] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_verify_one, jid, url, list(alts or []), co, t,
                               liveness_fetcher=liveness_fetcher, find_alt=find_alt)
                   for jid, url, alts, co, t in rows]
        for future in as_completed(futures):
            outcomes.append(future.result())

    alive_plain = [(now, o.job_id) for o in outcomes if o.kind == "alive" and o.new_url is None]
    promoted = [(o.new_url, o.new_alt_urls, now, o.job_id)
                for o in outcomes if o.kind == "alive" and o.new_url is not None]
    dead = [(cap, now, o.error, o.job_id) for o in outcomes if o.kind == "dead"]
    transient = [(o.error, o.job_id) for o in outcomes if o.kind == "transient"]

    with conn.transaction(), conn.cursor() as cur:
        if alive_plain:
            cur.executemany(
                "update jobs set url_status='alive', verified_at=%s, verify_error=null where id=%s",
                alive_plain)
        if promoted:
            cur.executemany(
                "update jobs set url=%s, alt_urls=%s, url_status='alive', verified_at=%s, "
                "verify_error=null where id=%s", promoted)
        if dead:
            cur.executemany(
                "update jobs set url_status='dead', verify_attempts=%s, verified_at=%s, "
                "verify_error=%s where id=%s", dead)
        if transient:
            cur.executemany(
                "update jobs set verify_attempts=verify_attempts+1, verify_error=%s where id=%s",
                transient)

    counts = {"alive": len(alive_plain), "promoted": len(promoted), "dead": len(dead),
              "transient": len(transient), "candidates": len(rows)}
    logger.info("verify summary: %s", counts)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        print(f"verify: {verify_urls(conn)}")
