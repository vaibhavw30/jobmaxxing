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
