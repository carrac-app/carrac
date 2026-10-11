"""
Thin wrappers — delegate to OpenAICompatProvider.

Historical name "kimi" is preserved for backwards-compat import paths;
the actual implementation is the generic OpenAI-compatible provider.
"""

from __future__ import annotations

import logging

from carrac.llm.openai_compat import OpenAICompatProvider

logger = logging.getLogger('carrac.ai.llm')


def _current_provider() -> OpenAICompatProvider:
    """One provider per call — cheap to instantiate, reads Flask config lazily."""
    return OpenAICompatProvider()


def stream_chat(messages, max_tokens=2000):
    """Stream text from an OpenAI-compatible backend.
    Legacy two-tuple shape:
        (text_chunk, None) per chunk
        (None, usage_dict) at end
    """
    provider = _current_provider()
    for event_type, payload, meta in provider.stream_chat(messages, max_tokens=max_tokens):
        if event_type == 'text':
            yield (payload, None)
        elif event_type == 'usage':
            yield (None, meta or {})


def stream_chat_with_tools(messages, tools=None, max_tokens=2000):
    """Stream with tool calling. Legacy three-tuple shape."""
    provider = _current_provider()
    for event_type, payload, meta in provider.stream_chat(
        messages, tools=tools, max_tokens=max_tokens
    ):
        if event_type == 'text':
            yield ('text', payload, None)
        elif event_type == 'tool_call':
            yield ('tool_call', None, meta)
        elif event_type == 'usage':
            yield ('usage', None, meta or {})
