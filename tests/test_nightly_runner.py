import sys

from jobmaxxing.scheduling.nightly import _subprocess_runner


def test_runner_ok_captures_output():
    r = _subprocess_runner("echo", [sys.executable, "-c", "print('hello nightly')"], timeout=30)
    assert r.status == "ok"
    assert r.exit_code == 0
    assert "hello nightly" in r.output


def test_runner_nonzero_exit_is_failed():
    r = _subprocess_runner("boom", [sys.executable, "-c", "import sys; sys.exit(3)"], timeout=30)
    assert r.status == "failed"
    assert r.exit_code == 3


def test_runner_timeout_is_timeout():
    r = _subprocess_runner("slow", [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert r.status == "timeout"
    assert r.exit_code is None


def test_shim_exposes_main():
    import jobmaxxing.nightly as shim
    from jobmaxxing.scheduling.nightly import main
    assert shim.main is main
