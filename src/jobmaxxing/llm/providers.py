import os

import anthropic
import openai

PROVIDER_KEYS = {
    "openai": "OPENAI_API_KEY",
    "xai": "XAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
PROVIDER_BASE_URLS = {
    "xai": "https://api.x.ai/v1",
}


def provider_available(provider: str) -> bool:
    """True if the provider's API key env var is set."""
    return bool(os.environ.get(PROVIDER_KEYS.get(provider, "")))


def _openai_compatible(provider, model, messages, max_tokens, response_format):
    init: dict = {"api_key": os.environ[PROVIDER_KEYS[provider]]}
    base_url = PROVIDER_BASE_URLS.get(provider)
    if base_url:
        init["base_url"] = base_url
    client = openai.OpenAI(**init)
    call: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if response_format:
        call["response_format"] = response_format
    resp = client.chat.completions.create(**call)
    return resp.choices[0].message.content


def _anthropic(provider, model, messages, max_tokens, response_format):
    client = anthropic.Anthropic(api_key=os.environ[PROVIDER_KEYS[provider]])
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    convo = [m for m in messages if m["role"] != "system"]
    resp = client.messages.create(
        model=model, system=system or None, messages=convo, max_tokens=max_tokens
    )
    return resp.content[0].text


_ADAPTERS = {"openai": _openai_compatible, "xai": _openai_compatible, "anthropic": _anthropic}


def call_provider(provider, model, messages, *, max_tokens, response_format=None) -> str:
    """Dispatch one completion to a provider. Raises if the provider is unknown or the SDK errors."""
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise ValueError(f"unknown provider: {provider!r}")
    return adapter(provider, model, messages, max_tokens, response_format)
