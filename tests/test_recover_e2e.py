"""Real DuckDuckGo + JSON-LD recovery against a live posting. Skipped unless JOBMAXXING_E2E=1
(mirrors the Workday/claude-cli e2e skips). Run on a residential IP:
JOBMAXXING_E2E=1 uv run pytest tests/test_recover_e2e.py -v"""

import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("JOBMAXXING_E2E") != "1",
                                reason="set JOBMAXXING_E2E=1 to run the live find-elsewhere e2e")


def test_live_recovery_finds_a_jobposting():
    from jobmaxxing.recovery.extract import extract_job_posting
    from jobmaxxing.recovery.recover import _default_fetcher
    from jobmaxxing.recovery.search import ddg_search

    results = ddg_search("Chegg Computational Linguist job", fetch_text=_default_fetcher)
    found = False
    for url in results:
        try:
            if extract_job_posting(_default_fetcher(url), source_url=url):
                found = True
                break
        except httpx.HTTPError:
            continue
    assert found, "expected at least one JobPosting JSON-LD among DDG results"
