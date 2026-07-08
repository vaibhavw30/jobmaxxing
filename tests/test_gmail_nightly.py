from jobmaxxing.scheduling.nightly import WORKER_NAMES, default_workers


def test_discover_gmail_runs_first_in_nightly_batch():
    # pure ingestion → runs before the scrapers/enrichers
    assert WORKER_NAMES[0] == "discover_gmail"
    assert "discover_gmail" in WORKER_NAMES


def test_default_workers_invoke_discover_gmail_module():
    names = [name for name, _argv in default_workers()]
    assert names[0] == "discover_gmail"
    argv = dict(default_workers())["discover_gmail"]
    assert argv[-2:] == ["-m", "jobmaxxing.discover_gmail"]
