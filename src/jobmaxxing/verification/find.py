"""Find a working alternative posting URL by reusing the recovery search/extract/match engine.

When a job's primary URL is dead, search the web for the same posting elsewhere (LinkedIn / job
board / ATS) and return a candidate URL ONLY if it is confidently matched to this job (recovery's
req-id / backlink / LLM check). The caller liveness-checks the returned URL before trusting it.
"""

from ..recovery.extract import extract_job_posting, workday_req_id
from ..recovery.match import match_job
from ..recovery.recover import _default_fetcher, _default_llm_confirm
from ..recovery.search import build_query, ddg_search


def find_alternative_url(company, title, original_url, *,
                         searcher=ddg_search, fetcher=_default_fetcher,
                         llm_confirm=_default_llm_confirm) -> str | None:
    job = {"company": company, "title": title, "url": original_url,
           "req_id": workday_req_id(original_url)}
    try:
        results = searcher(build_query(company, title), fetch_text=fetcher)
    except Exception:  # noqa: BLE001 - a search failure just means no alternative this round
        return None
    for result_url in results:
        try:
            cand = extract_job_posting(fetcher(result_url), source_url=result_url)
        except Exception:  # noqa: BLE001 - skip an unfetchable/unparseable candidate
            continue
        if cand and match_job(job, cand, llm_confirm=llm_confirm).accepted:
            return cand.url or cand.source_url or result_url
    return None
