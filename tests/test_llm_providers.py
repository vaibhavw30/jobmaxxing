import openai
import anthropic

from jobmaxxing.llm import providers


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeChatResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeOpenAIClient:
    last_init = None
    last_call = None

    def __init__(self, **kwargs):
        _FakeOpenAIClient.last_init = kwargs
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        _FakeOpenAIClient.last_call = kwargs
        return _FakeChatResp("swe")


class _FakeAnthropicBlock:
    def __init__(self, text):
        self.text = text


class _FakeAnthropicResp:
    def __init__(self, text):
        self.content = [_FakeAnthropicBlock(text)]


class _FakeAnthropicClient:
    last_init = None
    last_call = None

    def __init__(self, **kwargs):
        _FakeAnthropicClient.last_init = kwargs

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        _FakeAnthropicClient.last_call = kwargs
        return _FakeAnthropicResp("mle")


def test_provider_available_checks_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert providers.provider_available("openai") is False
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert providers.provider_available("openai") is True


def test_openai_adapter(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAIClient)
    out = providers.call_provider("openai", "gpt-4o-mini", [{"role": "user", "content": "hi"}], max_tokens=50)
    assert out == "swe"
    assert "base_url" not in _FakeOpenAIClient.last_init       # openai uses default base url
    assert _FakeOpenAIClient.last_init["api_key"] == "sk-x"
    assert _FakeOpenAIClient.last_call["model"] == "gpt-4o-mini"


def test_xai_adapter_sets_base_url(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-x")
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAIClient)
    out = providers.call_provider("xai", "grok-3-mini", [{"role": "user", "content": "hi"}], max_tokens=50)
    assert out == "swe"
    assert _FakeOpenAIClient.last_init["base_url"] == "https://api.x.ai/v1"
    assert _FakeOpenAIClient.last_init["api_key"] == "xai-x"


def test_anthropic_adapter_splits_system(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-x")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicClient)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    out = providers.call_provider("anthropic", "claude-3-5-haiku-latest", messages, max_tokens=50)
    assert out == "mle"
    assert _FakeAnthropicClient.last_call["system"] == "sys"
    assert _FakeAnthropicClient.last_call["messages"] == [{"role": "user", "content": "hi"}]
    assert _FakeAnthropicClient.last_call["max_tokens"] == 50


def test_anthropic_omits_system_when_absent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-x")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicClient)
    providers.call_provider("anthropic", "claude-3-5-haiku-latest",
                            [{"role": "user", "content": "hi"}], max_tokens=50)
    # No system message -> SDK NOT_GIVEN sentinel, never None (which serializes as null)
    assert _FakeAnthropicClient.last_call["system"] is anthropic.NOT_GIVEN


def test_openai_passes_response_format(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAIClient)
    providers.call_provider("openai", "gpt-4o-mini", [{"role": "user", "content": "hi"}],
                            max_tokens=50, response_format={"type": "json_object"})
    assert _FakeOpenAIClient.last_call["response_format"] == {"type": "json_object"}


def test_call_provider_unknown_provider_raises():
    import pytest
    with pytest.raises(ValueError):
        providers.call_provider("workday", "m", [{"role": "user", "content": "x"}], max_tokens=10)


def test_provider_available_claude_cli_checks_binary(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert providers.provider_available("claude-cli") is True
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    assert providers.provider_available("claude-cli") is False
