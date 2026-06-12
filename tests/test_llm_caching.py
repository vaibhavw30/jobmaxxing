import anthropic
import openai

from jobmaxxing.llm import client, providers


class _FakeOpenAIClient:
    last_call = None

    def __init__(self, **kwargs):
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        _FakeOpenAIClient.last_call = kwargs

        class _R:
            choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]

        return _R()


class _FakeAnthropicClient:
    last_call = None

    def __init__(self, **kwargs):
        pass

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        _FakeAnthropicClient.last_call = kwargs

        class _R:
            content = [type("B", (), {"text": "ok"})()]

        return _R()


def test_openai_prepends_cache_as_system(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAIClient)
    providers.call_provider("openai", "gpt-4o", [{"role": "user", "content": "hi"}], max_tokens=50, cache="BASE")
    msgs = _FakeOpenAIClient.last_call["messages"]
    assert msgs[0] == {"role": "system", "content": "BASE"}
    assert msgs[1] == {"role": "user", "content": "hi"}


def test_anthropic_caches_system_block(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-x")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicClient)
    providers.call_provider(
        "anthropic", "claude-sonnet-4-latest",
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}],
        max_tokens=50, cache="BASE",
    )
    system = _FakeAnthropicClient.last_call["system"]
    assert isinstance(system, list)
    assert system[0]["text"] == "BASE"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert any(b["text"] == "rules" for b in system)


def test_complete_threads_cache(monkeypatch):
    captured = {}

    def fake_call(provider, model, messages, **kw):
        captured.update(kw)
        return "ok"

    monkeypatch.setattr(client, "provider_available", lambda p: True)
    monkeypatch.setattr(client, "call_provider", fake_call)
    cfg = {"tasks": {"tailor": [{"provider": "anthropic", "model": "m"}]}}
    client.complete("tailor", [{"role": "user", "content": "x"}], max_tokens=10, cache="BASE", config=cfg)
    assert captured["cache"] == "BASE"
