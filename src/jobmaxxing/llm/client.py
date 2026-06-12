import logging

from .config import candidates_for, load_llm_config
from .providers import call_provider, provider_available

logger = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    """Raised when no configured LLM candidate could serve a request."""


def complete(task, messages, *, max_tokens, response_format=None, cache=None, config=None) -> str:
    """Try each configured (provider, model) for `task` in order. Skip providers with no
    API key; fall through on any error. Raise LLMUnavailable if none succeed."""
    cfg = config if config is not None else load_llm_config()
    candidates = candidates_for(task, cfg)
    tried: list[str] = []
    last_error: Exception | None = None
    for cand in candidates:
        provider, model = cand["provider"], cand["model"]
        if not provider_available(provider):
            logger.debug("llm: skipping %s (no API key)", provider)  # normal when not all keys are set
            continue
        tried.append(f"{provider}/{model}")
        try:
            return call_provider(
                provider, model, messages,
                max_tokens=max_tokens, response_format=response_format, cache=cache,
            )
        except ValueError:
            raise  # config/programming error (e.g. unknown provider) — surface it, don't mask as transient
        except Exception as exc:  # noqa: BLE001 - transient provider fallback is the whole point
            last_error = exc
            logger.warning("llm: %s/%s failed: %s", provider, model, exc)
    raise LLMUnavailable(
        f"no llm candidate succeeded for task {task!r}; "
        f"tried={tried or 'none (all skipped/no key)'}: {last_error}"
    )
