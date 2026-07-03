"""Live JobSpy scrape — skipped unless JOBMAXXING_E2E=1 and the `discovery` extra is installed."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("JOBMAXXING_E2E") != "1", reason="set JOBMAXXING_E2E=1 for the live JobSpy scrape")


def test_live_indeed_scrape_parses():
    pytest.importorskip("jobspy")
    from jobmaxxing.discovery.jobspy_source import _jobspy_scrape, parse_jobspy
    rows = _jobspy_scrape({"site": "indeed", "term": "software engineer intern",
                           "location": "United States", "results_wanted": 5,
                           "country_indeed": "USA", "job_type": "internship"})
    assert isinstance(rows, list)
    records = parse_jobspy(rows, site="indeed")   # >= 0; network-dependent
    assert isinstance(records, list)
