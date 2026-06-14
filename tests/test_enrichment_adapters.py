from jobmaxxing.enrichment.adapters import adapter_for, GreenhouseAdapter, SUPPORTED_HOSTS_SQL


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
