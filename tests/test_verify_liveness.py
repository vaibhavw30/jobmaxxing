from jobmaxxing.verification.liveness import check_liveness


def _fixed(status):
    return lambda url: status


def test_2xx_and_3xx_final_are_alive():
    assert check_liveness("https://x", fetcher=_fixed(200)).kind == "alive"
    assert check_liveness("https://x", fetcher=_fixed(301)).kind == "alive"


def test_404_and_410_are_dead():
    assert check_liveness("https://x", fetcher=_fixed(404)).kind == "dead"
    assert check_liveness("https://x", fetcher=_fixed(410)).kind == "dead"


def test_other_statuses_are_transient():
    for s in (403, 429, 500, 503):
        assert check_liveness("https://x", fetcher=_fixed(s)).kind == "transient"


def test_fetcher_exception_is_transient():
    def boom(url):
        raise RuntimeError("timeout")
    result = check_liveness("https://x", fetcher=boom)
    assert result.kind == "transient"
    assert result.status is None
