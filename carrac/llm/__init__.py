"""
Unified LLM provider layer for Carrac.

Centralises Anthropic / OpenAI-compatible / xAI Grok streaming, cost
accounting, and SSE parsing. Used by:
  - carrac/ai/* (web AI chat, streaming generators)
  - morning_engine/* (blocking chat, cumulative cost)

Existing module-level entry points in carrac/ai/{anthropic,kimi,llm}_client.py
and morning_engine/{anthropic,grok}_client.py are kept as thin wrappers for
backwards compatibility — they delegate to providers from this package.
"""

from carrac.llm.base import (
    LLMProvider,
    TrackedLLMProvider,
    StreamEvent,
    Usage,
    LLMError,
)

__all__ = [
    'LLMProvider',
    'TrackedLLMProvider',
    'StreamEvent',
    'Usage',
    'LLMError',
]
