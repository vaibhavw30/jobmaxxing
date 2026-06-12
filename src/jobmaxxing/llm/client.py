import logging

from .config import candidates_for, load_llm_config
from .providers import call_provider, provider_available

logger = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    """Raised when no configured LLM candidate could serve a request."""


def complete(task, messages, *, max_tokens, response_format=None, config=None) -> str:
    """Try each configured (provider, model) for `task` in order. Skip providers with no
    API key; fall through on any error. Raise LLMUnavailable if none succeed."""
    cfg = config if config is not None else load_llm_config()
    candidates = candidates_for(task, cfg)
    last_error: Exception | None = None
    for cand in candidates:
        provider, model = cand["provider"], cand["model"]
        if not provider_available(provider):
            logger.warning("llm: skipping %s (no API key)", provider)
            continue
        try:
            return call_provider(provider, model, messages, max_tokens=max_tokens, response_format=response_format)
        except Exception as exc:  # noqa: BLE001 - provider fallback is the whole point
            last_error = exc
            logger.warning("llm: %s/%s failed: %s", provider, model, exc)
    raise LLMUnavailable(f"no llm candidate succeeded for task {task!r}: {last_error}")
