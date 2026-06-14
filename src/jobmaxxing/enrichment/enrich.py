import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import httpx
import psycopg

from ..config import load_settings
from ..fetch import fetch_json as fetch_json_default
from .adapters import adapter_for, SUPPORTED_HOSTS_SQL

logger = logging.getLogger(__name__)

def _is_permanent_http(code: int) -> bool:
    """4xx except 429 means retrying won't help -> permanent. 429 and 5xx -> transient."""
    return 400 <= code < 500 and code != 429


def classify_error(exc: Exception) -> str:
    """'permanent' (never retry) or 'transient' (retry until cap)."""
    if isinstance(exc, httpx.HTTPStatusError) and _is_permanent_http(exc.response.status_code):
        return "permanent"
    return "transient"  # 429, 5xx, timeouts, connection errors


@dataclass
class Outcome:
    job_id: object
    kind: str           # "enriched" | "permanent" | "transient"
    description: str | None
    error: str | None


def _fetch_one(job_id, url: str, fetch_json) -> Outcome:
    """Fetch + parse one row's JD. Pure w.r.t. the DB; isolates all errors."""
    adapter = adapter_for(url)
    if adapter is None:
        return Outcome(job_id, "permanent", None, f"no adapter for {url}")
    try:
        payload = fetch_json(adapter.api_url(url))
    except Exception as exc:  # noqa: BLE001 - classify, never propagate
        return Outcome(job_id, classify_error(exc), None, f"{type(exc).__name__}: {exc}"[:500])
    description = adapter.parse(payload, url)
    if not description:
        return Outcome(job_id, "permanent", None, "no description in payload")
    return Outcome(job_id, "enriched", description, None)


def enrich_new(
    conn: psycopg.Connection,
    *,
    max_fetches: int = 500,
    max_workers: int = 8,
    cap: int = 3,
    fetch_json=fetch_json_default,
) -> dict:
    """Fetch JDs for supported, description-less rows. Bounded-concurrent; batched write.

    Returns {enriched, permanent_failed, transient_failed, candidates}.
    """
    rows = conn.execute(
        "select id, url from jobs "
        "where coalesce(description, '') = '' "
        "and route_method is distinct from 'manual' "
        "and enrich_attempts < %s "
        "and url ~* %s "
        "order by scraped_at desc "
        "limit %s",
        (cap, SUPPORTED_HOSTS_SQL, max_fetches),
    ).fetchall()
    if not rows:
        return {"enriched": 0, "permanent_failed": 0, "transient_failed": 0, "candidates": 0}

    outcomes: list[Outcome] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_one, job_id, url, fetch_json) for job_id, url in rows]
        for future in as_completed(futures):
            outcomes.append(future.result())

    enriched = [(o.description, o.job_id) for o in outcomes if o.kind == "enriched"]
    permanent = [(cap, o.error, o.job_id) for o in outcomes if o.kind == "permanent"]
    transient = [(o.error, o.job_id) for o in outcomes if o.kind == "transient"]

    with conn.transaction(), conn.cursor() as cur:
        if enriched:
            # Note: enrich_attempts is intentionally NOT reset here. A successfully
            # enriched row now has a non-empty description, so coalesce(description,'')=''
            # excludes it from the candidate query regardless of its attempt count.
            cur.executemany(
                "update jobs set description=%s, enriched_at=now(), enrich_error=null where id=%s",
                enriched,
            )
        if permanent:
            cur.executemany(
                "update jobs set enrich_attempts=%s, enrich_error=%s where id=%s",
                permanent,
            )
        if transient:
            cur.executemany(
                "update jobs set enrich_attempts=enrich_attempts+1, enrich_error=%s where id=%s",
                transient,
            )

    counts = {
        "enriched": len(enriched),
        "permanent_failed": len(permanent),
        "transient_failed": len(transient),
        "candidates": len(rows),
    }
    logger.info("enrich summary: %s", counts)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        counts = enrich_new(conn)
        print(f"enriched: {counts}")
