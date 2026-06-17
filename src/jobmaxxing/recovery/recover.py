"""Find-elsewhere JD recovery worker. Run LOCALLY: python -m jobmaxxing.recover_jd"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import httpx
import psycopg

from ..config import load_settings
from .extract import extract_job_posting, workday_req_id
from .match import match_job
from .search import build_query, ddg_search

logger = logging.getLogger(__name__)
_HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")}


def _default_fetcher(url: str) -> str:
    r = httpx.get(url, headers=_HEADERS, timeout=20.0, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _default_llm_confirm(job: dict, cand) -> bool:
    """One cheap schema-gated 'same posting?' check. Any failure -> False (prefer a safe miss)."""
    from ..llm.client import LLMUnavailable, complete
    messages = [
        {"role": "system", "content": ("Decide whether two postings are the SAME job at the SAME company. "
                                        'Respond with STRICT JSON only: {"same": true|false}. No prose.')},
        {"role": "user", "content": (f"A) {job.get('company')} — {job.get('title')}\n"
                                      f"B) {cand.company} — {cand.title}\n"
                                      f"B description:\n{(cand.description or '')[:600]}")},
    ]
    try:
        text = complete("route", messages, max_tokens=50, response_format={"type": "json_object"})
    except LLMUnavailable:
        return False
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return False
    try:
        return json.loads(m.group(0)).get("same") is True
    except (ValueError, TypeError):
        return False


@dataclass
class _Outcome:
    job_id: object
    description: str | None     # set when recovered
    error: str | None           # set when missed


def _recover_one(job_id, company, title, url, *, searcher, fetcher, llm_confirm) -> _Outcome:
    job = {"company": company, "title": title, "url": url, "req_id": workday_req_id(url)}
    try:
        results = searcher(build_query(company, title), fetch_text=fetcher)
    except Exception as exc:  # noqa: BLE001 - a search failure just misses this job
        return _Outcome(job_id, None, f"search: {type(exc).__name__}")
    for result_url in results:
        try:
            cand = extract_job_posting(fetcher(result_url), source_url=result_url)
        except Exception:  # noqa: BLE001 - skip an unfetchable/unparseable candidate
            continue
        if cand and match_job(job, cand, llm_confirm=llm_confirm).accepted:
            return _Outcome(job_id, cand.description, None)
    return _Outcome(job_id, None, "no match")


def recover_new(conn, *, max_jobs=100, max_workers=3, cap=2,
                searcher=ddg_search, fetcher=_default_fetcher, llm_confirm=_default_llm_confirm) -> dict:
    """Recover JDs for relevant, JD-less Workday rows. Returns {recovered, missed, candidates}.

    `cap` is the max `recover_attempts` before a job is no longer selected (give-up bound)."""
    rows = conn.execute(
        "select id, company, title, url from jobs "
        "where coalesce(description,'')='' "
        "and resume_type is not null "
        "and route_method is distinct from 'manual' "
        "and recover_attempts < %s "
        "and url ilike '%%myworkdayjobs%%' "
        "order by scraped_at desc limit %s",
        (cap, max_jobs),
    ).fetchall()
    conn.commit()  # release the read's snapshot/lock BEFORE any (slow) network I/O
    if not rows:
        return {"recovered": 0, "missed": 0, "candidates": 0}

    outcomes: list[_Outcome] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_recover_one, jid, co, t, u,
                               searcher=searcher, fetcher=fetcher, llm_confirm=llm_confirm)
                   for jid, co, t, u in rows]
        for future in as_completed(futures):
            outcomes.append(future.result())

    recovered = [(o.description, o.job_id) for o in outcomes if o.description]
    missed = [(o.error, o.job_id) for o in outcomes if not o.description]
    if recovered or missed:
        with conn.transaction(), conn.cursor() as cur:
            if recovered:
                cur.executemany(
                    "update jobs set description=%s, jd_source='recovered', "
                    "resume_type=null, route_method=null where id=%s",
                    recovered,
                )
            if missed:
                cur.executemany(
                    "update jobs set recover_attempts=recover_attempts+1, recover_error=%s where id=%s",
                    missed,
                )
    counts = {"recovered": len(recovered), "missed": len(missed), "candidates": len(rows)}
    logger.info("recover summary: %s", counts)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        print(f"recovered: {recover_new(conn)}")
