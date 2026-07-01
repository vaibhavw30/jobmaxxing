"""CLI dispatch tests for `python -m jobmaxxing.route` (no real DB — psycopg.connect is patched)."""

import sys

import jobmaxxing.routing.route as R


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_common(monkeypatch, captured):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5432/db")
    monkeypatch.setattr(R.psycopg, "connect", lambda url: _FakeConn())
    monkeypatch.setattr(R, "route_new", lambda conn, **kw: captured.update(kw) or {"rules": 0})
    monkeypatch.setattr(R, "set_manual", lambda conn, jid, rt: captured.update(set=(jid, rt)))


def test_cli_no_llm_flag_sets_no_llm_true(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)
    monkeypatch.setattr(sys, "argv", ["route", "--no-llm"])
    R.main()
    assert captured.get("no_llm") is True


def test_cli_plain_route_sets_no_llm_false(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)
    monkeypatch.setattr(sys, "argv", ["route"])
    R.main()
    assert captured.get("no_llm") is False


def test_cli_set_subcommand_unaffected(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)
    monkeypatch.setattr(sys, "argv", ["route", "set", "abc-123", "swe"])
    R.main()
    assert captured.get("set") == ("abc-123", "swe")
    assert "no_llm" not in captured        # route_new not called for `set`
