import pytest

from jobmaxxing.llm import client
from jobmaxxing.llm.client import LLMUnavailable, complete

CONFIG = {
    "tasks": {
        "route": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "xai", "model": "grok-3-mini"},
            {"provider": "anthropic", "model": "claude-3-5-haiku-latest"},
        ]
    }
}
MESSAGES = [{"role": "user", "content": "hi"}]


def test_uses_first_available_provider(monkeypatch):
    monkeypatch.setattr(client, "provider_available", lambda p: True)
    monkeypatch.setattr(client, "call_provider", lambda p, m, msgs, **kw: f"{p}:ok")
    assert complete("route", MESSAGES, max_tokens=50, config=CONFIG) == "openai:ok"


def test_skips_provider_without_key_and_falls_through(monkeypatch):
    monkeypatch.setattr(client, "provider_available", lambda p: p != "openai")
    monkeypatch.setattr(client, "call_provider", lambda p, m, msgs, **kw: f"{p}:ok")
    assert complete("route", MESSAGES, max_tokens=50, config=CONFIG) == "xai:ok"


def test_falls_through_on_error(monkeypatch):
    monkeypatch.setattr(client, "provider_available", lambda p: True)

    def flaky(p, m, msgs, **kw):
        if p in ("openai", "xai"):
            raise RuntimeError("rate limit")
        return f"{p}:ok"

    monkeypatch.setattr(client, "call_provider", flaky)
    assert complete("route", MESSAGES, max_tokens=50, config=CONFIG) == "anthropic:ok"


def test_raises_when_all_fail(monkeypatch):
    monkeypatch.setattr(client, "provider_available", lambda p: True)
    monkeypatch.setattr(client, "call_provider", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(LLMUnavailable):
        complete("route", MESSAGES, max_tokens=50, config=CONFIG)


def test_raises_when_no_candidates(monkeypatch):
    with pytest.raises(LLMUnavailable):
        complete("route", MESSAGES, max_tokens=50, config={"tasks": {}})


def test_raises_when_all_skipped_no_key(monkeypatch):
    monkeypatch.setattr(client, "provider_available", lambda p: False)
    monkeypatch.setattr(client, "call_provider", lambda *a, **k: "should-not-be-called")
    with pytest.raises(LLMUnavailable):
        complete("route", MESSAGES, max_tokens=50, config=CONFIG)


def test_config_error_propagates_as_valueerror(monkeypatch):
    # an unknown provider name is a config bug, not a transient failure -> must surface
    monkeypatch.setattr(client, "provider_available", lambda p: True)

    def bad(p, m, msgs, **kw):
        raise ValueError("unknown provider")

    monkeypatch.setattr(client, "call_provider", bad)
    with pytest.raises(ValueError):
        complete("route", MESSAGES, max_tokens=50, config=CONFIG)
