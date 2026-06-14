from jobmaxxing.enrichment.adapters import adapter_for, GreenhouseAdapter, SUPPORTED_HOSTS_SQL
from jobmaxxing.enrichment.adapters import LeverAdapter
from jobmaxxing.enrichment.adapters import AshbyAdapter


def test_greenhouse_matches_and_api_url():
    url = "https://job-boards.greenhouse.io/incidentiq/jobs/7724767003"
    a = adapter_for(url)
    assert a is GreenhouseAdapter
    assert a.api_url(url) == (
        "https://boards-api.greenhouse.io/v1/boards/incidentiq/jobs/7724767003?content=true"
    )


def test_greenhouse_parse_unescapes_html_content():
    payload = {"content": "&lt;p&gt;Build &amp; ship&lt;/p&gt;"}
    assert GreenhouseAdapter.parse(payload, "https://job-boards.greenhouse.io/x/jobs/1") == "<p>Build & ship</p>"


def test_greenhouse_parse_returns_none_when_no_content():
    assert GreenhouseAdapter.parse({}, "https://job-boards.greenhouse.io/x/jobs/1") is None


def test_unsupported_host_has_no_adapter():
    assert adapter_for("https://comcast.wd5.myworkdayjobs.com/en-US/x/job/y/z_R1") is None


def test_supported_hosts_sql_covers_all_four():
    for frag in ("greenhouse", "lever", "ashbyhq", "smartrecruiters"):
        assert frag in SUPPORTED_HOSTS_SQL


def test_lever_matches_strips_apply_suffix_in_api_url():
    url = "https://jobs.lever.co/waabi/62700386-b9db-4c78-aec3-5ef59cbe841e/apply"
    a = adapter_for(url)
    assert a is LeverAdapter
    assert a.api_url(url) == (
        "https://api.lever.co/v0/postings/waabi/62700386-b9db-4c78-aec3-5ef59cbe841e?mode=json"
    )


def test_lever_parse_uses_description_plain():
    assert LeverAdapter.parse({"descriptionPlain": "Build robots"}, "u") == "Build robots"


def test_lever_parse_returns_none_when_empty():
    assert LeverAdapter.parse({"descriptionPlain": ""}, "u") is None


_ASHBY_URL = "https://jobs.ashbyhq.com/replit/12737078-74c7-4e63-98a7-5e8da1e9deb1/application"


def test_ashby_matches_and_api_url_is_org_board():
    a = adapter_for(_ASHBY_URL)
    assert a is AshbyAdapter
    assert a.api_url(_ASHBY_URL) == (
        "https://api.ashbyhq.com/posting-api/job-board/replit?includeCompensation=true"
    )


def test_ashby_parse_selects_posting_by_id_from_url():
    payload = {"jobs": [
        {"id": "other-uuid", "descriptionPlain": "no"},
        {"id": "12737078-74c7-4e63-98a7-5e8da1e9deb1", "descriptionPlain": "Do X at Replit"},
    ]}
    assert AshbyAdapter.parse(payload, _ASHBY_URL) == "Do X at Replit"


def test_ashby_parse_returns_none_when_posting_absent():
    assert AshbyAdapter.parse({"jobs": [{"id": "z", "descriptionPlain": "x"}]}, _ASHBY_URL) is None
