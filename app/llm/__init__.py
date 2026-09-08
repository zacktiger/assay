"""Provider selection.

The active provider is a module-level singleton so the API layer never
constructs one per request, and so tests can swap in a double.
"""
from __future__ import annotations

from app.config import settings
from app.llm.base import LLMProvider, LLMResult
from app.llm.stub import ScriptedProvider, StubProvider

_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        if settings.llm_provider == "anthropic":
            from app.llm.anthropic_provider import AnthropicProvider

            _provider = AnthropicProvider()
        else:
            _provider = StubProvider()
    return _provider


def set_provider(provider: LLMProvider | None) -> None:
    """Swap the active provider. Passing None restores the configured default."""
    global _provider
    _provider = provider


__all__ = [
    "LLMProvider",
    "LLMResult",
    "ScriptedProvider",
    "StubProvider",
    "get_provider",
    "set_provider",
]
