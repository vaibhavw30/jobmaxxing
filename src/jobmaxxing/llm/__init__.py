"""Provider-agnostic LLM access layer."""

from .client import LLMUnavailable, complete

__all__ = ["complete", "LLMUnavailable"]
