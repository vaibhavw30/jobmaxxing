import subprocess

import openai
import anthropic
import pytest

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


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_claude_cli_happy_path_and_env_strips_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-should-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(returncode=0, stdout="  RESULT TEXT \n")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}]
    out = providers.call_provider("claude-cli", "sonnet", messages, max_tokens=4000, cache="BASE")
    assert out == "RESULT TEXT"
    # command: model + tools-off + system flag
    assert captured["cmd"][:5] == ["claude", "-p", "--model", "sonnet", "--allowedTools"]
    assert "--system-prompt" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--system-prompt") + 1] == "SYS"
    # stdin prompt = cache then user message
    assert captured["kwargs"]["input"] == "BASE\n\nUSR"
    # THE GUARANTEE: the child env has no ANTHROPIC_API_KEY (forces subscription auth)
    assert "ANTHROPIC_API_KEY" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["env"]["PATH"] == "/usr/bin"   # other env preserved


def test_claude_cli_omits_system_flag_when_no_system(monkeypatch):
    captured = {}
    monkeypatch.setattr(providers.subprocess, "run",
                        lambda cmd, **kw: captured.update(cmd=cmd) or _FakeProc(0, "ok"))
    providers.call_provider("claude-cli", "sonnet", [{"role": "user", "content": "hi"}], max_tokens=10)
    assert "--system-prompt" not in captured["cmd"]


def test_claude_cli_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(providers.subprocess, "run",
                        lambda cmd, **kw: _FakeProc(returncode=1, stdout="", stderr="not logged in"))
    with pytest.raises(RuntimeError, match="not logged in"):
        providers.call_provider("claude-cli", "sonnet", [{"role": "user", "content": "hi"}], max_tokens=10)


def test_claude_cli_timeout_raises(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(providers.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        providers.call_provider("claude-cli", "sonnet", [{"role": "user", "content": "hi"}], max_tokens=10)


def test_claude_cli_empty_output_raises(monkeypatch):
    monkeypatch.setattr(providers.subprocess, "run",
                        lambda cmd, **kw: _FakeProc(returncode=0, stdout="   \n"))
    with pytest.raises(RuntimeError, match="empty"):
        providers.call_provider("claude-cli", "sonnet", [{"role": "user", "content": "hi"}], max_tokens=10)
