from datetime import datetime, timezone

from jobmaxxing.scheduling.nightly import RunResult, run_nightly


def _ok(name):
    return RunResult(name=name, status="ok", exit_code=0, duration_s=0.1, output=f"{name} done\n")


def test_runs_workers_in_order_and_notifies_once():
    calls = []
    notifications = []

    def runner(name, argv, timeout):
        calls.append(name)
        return _ok(name)

    workers = [("a", ["x"]), ("b", ["y"]), ("c", ["z"])]
    report = run_nightly(workers, runner=runner, notifier=lambda t, b: notifications.append((t, b)),
                         db_delta=lambda since: 12, now=datetime(2026, 7, 7, tzinfo=timezone.utc))

    assert calls == ["a", "b", "c"]                     # order preserved
    assert len(notifications) == 1                      # exactly one notification
    assert notifications[0][0] == "jobmaxxing nightly ✓"
    assert "12 new postings" in notifications[0][1]
    assert report["new_postings"] == 12
    assert [r.status for r in report["results"]] == ["ok", "ok", "ok"]


def test_failsoft_a_raising_runner_does_not_abort_the_batch():
    calls = []

    def runner(name, argv, timeout):
        calls.append(name)
        if name == "b":
            raise RuntimeError("worker crashed")
        return _ok(name)

    workers = [("a", ["x"]), ("b", ["y"]), ("c", ["z"])]
    report = run_nightly(workers, runner=runner, notifier=lambda t, b: None,
                         db_delta=lambda since: 0, now=datetime(2026, 7, 7, tzinfo=timezone.utc))

    assert calls == ["a", "b", "c"]                     # c still ran after b raised
    statuses = {r.name: r.status for r in report["results"]}
    assert statuses == {"a": "ok", "b": "failed", "c": "ok"}
    assert report["title"] == "jobmaxxing nightly ✗"


def test_db_delta_failure_yields_none_count_and_still_notifies():
    notifications = []

    def bad_delta(since):
        raise RuntimeError("db down")

    report = run_nightly([("a", ["x"])], runner=lambda n, a, t: _ok(n),
                         notifier=lambda t, b: notifications.append((t, b)),
                         db_delta=bad_delta, now=datetime(2026, 7, 7, tzinfo=timezone.utc))

    assert report["new_postings"] is None
    assert len(notifications) == 1                      # notification still fired
    assert "new postings" not in notifications[0][1]


def test_notifier_failure_is_swallowed():
    def boom(title, body):
        raise RuntimeError("osascript missing")

    # must not raise
    report = run_nightly([("a", ["x"])], runner=lambda n, a, t: _ok(n), notifier=boom,
                         db_delta=lambda since: 1, now=datetime(2026, 7, 7, tzinfo=timezone.utc))
    assert report["results"][0].status == "ok"


def test_writes_each_result_to_the_log(tmp_path):
    log = tmp_path / "nightly.log"
    run_nightly([("a", ["x"]), ("b", ["y"])], runner=lambda n, a, t: _ok(n),
                notifier=lambda t, b: None, db_delta=lambda since: 0,
                now=datetime(2026, 7, 7, tzinfo=timezone.utc), log_file=log)
    text = log.read_text()
    assert "a: ok" in text and "b: ok" in text
    assert "a done" in text and "b done" in text
