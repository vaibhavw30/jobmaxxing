from jobmaxxing.enrichment.workday import workday_cxs_url, workday_host, parse_workday


def test_cxs_url_basic():
    u = "https://micron.wd1.myworkdayjobs.com/External/job/San-Jose-CA/Intern-ASIC_JR84107"
    assert workday_cxs_url(u) == (
        "https://micron.wd1.myworkdayjobs.com/wday/cxs/micron/External/job/San-Jose-CA/Intern-ASIC_JR84107"
    )


def test_cxs_url_strips_locale_prefix():
    u = "https://thales.wd3.myworkdayjobs.com/en-US/Careers/job/Glasgow/SW-Apprentice_R0298405"
    assert workday_cxs_url(u) == (
        "https://thales.wd3.myworkdayjobs.com/wday/cxs/thales/Careers/job/Glasgow/SW-Apprentice_R0298405"
    )


def test_cxs_url_non_workday_is_none():
    assert workday_cxs_url("https://job-boards.greenhouse.io/acme/jobs/1") is None


def test_workday_host():
    assert workday_host("https://psu.wd1.myworkdayjobs.com/PSU_Staff/job/Berks/Intern_REQ1") == (
        "psu.wd1.myworkdayjobs.com"
    )
    assert workday_host("https://x.greenhouse.io/y") is None


def test_parse_workday_extracts_html_description():
    payload = {"jobPostingInfo": {"jobDescription": "<p>Build chips</p>"}}
    assert parse_workday(payload) == "<p>Build chips</p>"


def test_parse_workday_none_when_absent_or_empty():
    assert parse_workday({"jobPostingInfo": {}}) is None
    assert parse_workday({}) is None
    assert parse_workday({"jobPostingInfo": {"jobDescription": ""}}) is None


import pytest

from jobmaxxing.enrichment.workday import (
    WorkdayBlocked, WorkdayNotFound, WorkdayTransient,
    _classify_status, _looks_like_challenge,
)


def test_classify_status_ok_returns_none():
    assert _classify_status(200) is None


@pytest.mark.parametrize("code", [403, 429, 503])
def test_classify_status_blocked(code):
    with pytest.raises(WorkdayBlocked):
        _classify_status(code)


@pytest.mark.parametrize("code", [404, 410])
def test_classify_status_not_found(code):
    with pytest.raises(WorkdayNotFound):
        _classify_status(code)


def test_classify_status_other_is_transient():
    with pytest.raises(WorkdayTransient):
        _classify_status(500)


def test_looks_like_challenge():
    assert _looks_like_challenge("Just a moment...") is True
    assert _looks_like_challenge("Attention Required! | Cloudflare") is True
    assert _looks_like_challenge("Software Engineer Intern - Micron") is False
    assert _looks_like_challenge("") is False


from jobmaxxing.enrichment.workday import fetch_workday_one

_PAYLOAD = {"jobPostingInfo": {"jobDescription": "<p>Real JD with enough words</p>"}}
_URL = "https://acme.wd5.myworkdayjobs.com/Careers/job/NYC/Intern_R1"


class FakeFetcher:
    """Drives each tier with a queued behavior: a dict -> returned, an Exception -> raised."""
    def __init__(self, plain=None, context=None, render=None):
        self._plan = {"plain": plain, "context": context, "render": render}
        self.calls = []

    def _do(self, tier):
        self.calls.append(tier)
        b = self._plan[tier]
        if isinstance(b, Exception):
            raise b
        if b is None:
            raise AssertionError(f"tier {tier} unexpectedly called")
        return b

    def fetch_plain(self, cxs_url):
        return self._do("plain")

    def fetch_via_context(self, host, cxs_url):
        return self._do("context")

    def fetch_via_render(self, job_url):
        return self._do("render")


def test_tier0_success_skips_other_tiers():
    f = FakeFetcher(plain=_PAYLOAD)
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "enriched"
    assert out.description == "<p>Real JD with enough words</p>"
    assert f.calls == ["plain"]


def test_escalates_to_context_on_block():
    f = FakeFetcher(plain=WorkdayBlocked("403"), context=_PAYLOAD)
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "enriched"
    assert f.calls == ["plain", "context"]


def test_escalates_to_render_on_block():
    f = FakeFetcher(plain=WorkdayBlocked("403"), context=WorkdayBlocked("403"), render=_PAYLOAD)
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "enriched"
    assert f.calls == ["plain", "context", "render"]


def test_blocked_all_tiers_is_transient():
    f = FakeFetcher(plain=WorkdayBlocked("x"), context=WorkdayBlocked("x"), render=WorkdayBlocked("x"))
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "transient"
    assert f.calls == ["plain", "context", "render"]


def test_not_found_stops_immediately_permanent():
    f = FakeFetcher(plain=WorkdayNotFound("404"))
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "permanent"
    assert f.calls == ["plain"]


def test_transient_stops_immediately():
    f = FakeFetcher(plain=WorkdayTransient("timeout"))
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "transient"
    assert f.calls == ["plain"]


def test_payload_without_description_is_permanent():
    f = FakeFetcher(plain={"jobPostingInfo": {}})
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "permanent"


def test_unrecognized_url_is_permanent_without_fetching():
    f = FakeFetcher()
    out = fetch_workday_one("j1", "https://x.greenhouse.io/y", f)
    assert out.kind == "permanent"
    assert f.calls == []


def test_unexpected_fetcher_error_is_transient_not_crash():
    # A buggy/crashing fetcher raising a non-Workday exception must be isolated to a
    # transient outcome, never propagate and crash the whole shard.
    f = FakeFetcher(plain=ValueError("buggy fetcher"))
    out = fetch_workday_one("j1", _URL, f)
    assert out.kind == "transient"
    assert "ValueError" in out.error
    assert f.calls == ["plain"]


def test_myworkdaysite_cxs_url_basic():
    u = "https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/Southfield-Michigan-US/Intern---Engineering-Optics_R00243622"
    assert workday_cxs_url(u) == (
        "https://magna.wd3.myworkdayjobs.com/wday/cxs/magna/Magna/job/"
        "Southfield-Michigan-US/Intern---Engineering-Optics_R00243622"
    )


def test_myworkdaysite_cxs_url_strips_locale_prefix():
    u = ("https://wd1.myworkdaysite.com/en-US/recruiting/parexel/Parexel_External_Careers/"
         "job/United-Kingdom-Sheffield-Remote/Intern_R0000038395-1")
    assert workday_cxs_url(u) == (
        "https://parexel.wd1.myworkdayjobs.com/wday/cxs/parexel/Parexel_External_Careers/"
        "job/United-Kingdom-Sheffield-Remote/Intern_R0000038395-1"
    )


def test_myworkdaysite_host():
    u = "https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/Southfield-Michigan-US/Intern---Engineering-Optics_R00243622"
    assert workday_host(u) == "magna.wd3.myworkdayjobs.com"


def test_myworkdaysite_and_myworkdayjobs_same_identity_are_equivalent():
    # Same tenant/wd/site/rest via the two different public-domain shapes -> identical
    # host/cxs output. This is the sharding + Cloudflare-clearance-reuse invariant.
    site_url = "https://wd2.myworkdaysite.com/recruiting/acme/Careers/job/NYC/Intern_R1"
    jobs_url = "https://acme.wd2.myworkdayjobs.com/Careers/job/NYC/Intern_R1"
    assert workday_host(site_url) == workday_host(jobs_url) == "acme.wd2.myworkdayjobs.com"
    assert workday_cxs_url(site_url) == workday_cxs_url(jobs_url) == (
        "https://acme.wd2.myworkdayjobs.com/wday/cxs/acme/Careers/job/NYC/Intern_R1"
    )


def test_myworkdaysite_missing_recruiting_segment_is_none():
    # Not the recognized shape (no "recruiting/" path segment) -> unrecognized, not mangled.
    u = "https://wd2.myworkdaysite.com/acme/Careers/job/NYC/Intern_R1"
    assert workday_host(u) is None
    assert workday_cxs_url(u) is None
