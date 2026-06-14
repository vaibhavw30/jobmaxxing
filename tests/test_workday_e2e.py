"""End-to-end: real Playwright vs. live Workday. Skipped unless JOBMAXXING_E2E=1 (like the
pdflatex tests) — never runs in CI/normal pytest. Run locally with the headless extra."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("JOBMAXXING_E2E") != "1",
    reason="set JOBMAXXING_E2E=1 (and install the headless extra) to run live Workday e2e",
)

# A Tier-0-reachable tenant (edu/gov often serve cxs directly). Replace if it goes stale.
_TIER0_URL = "https://psu.wd1.myworkdayjobs.com/PSU_Staff/job/Penn-State-Berks/Part-Time---Physics---Internship_REQ_0000068406-1"


def test_live_workday_tier0_enriches():
    from jobmaxxing.enrichment.playwright_fetcher import PlaywrightFetcher
    from jobmaxxing.enrichment.workday import fetch_workday_one

    fetcher = PlaywrightFetcher()
    try:
        out = fetch_workday_one("e2e", _TIER0_URL, fetcher)
    finally:
        fetcher.close()
    # A live posting enriches with a non-trivial HTML description; a stale one is permanent.
    assert out.kind in {"enriched", "permanent"}
    if out.kind == "enriched":
        assert out.description and len(out.description) > 200
