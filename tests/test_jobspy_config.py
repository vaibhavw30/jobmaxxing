from jobmaxxing.discovery.jobspy_source import load_jobspy_config


def test_shipped_config_parses_with_indeed_and_linkedin():
    cfg = load_jobspy_config()
    assert "indeed" in cfg["sites"] and "linkedin" in cfg["sites"]
    assert cfg["search_terms"]                       # non-empty
    assert cfg["job_type"] == "internship"
    assert cfg["results_wanted"]["linkedin"] <= 100  # bounded for a single residential IP
