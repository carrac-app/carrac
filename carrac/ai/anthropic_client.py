"""
Thin wrappers for backwards compatibility — delegate to AnthropicProvider.

The canonical implementation lives in carrac/llm/anthropic.py.
This module preserves the historical module-level function API used by
`carrac/ai/llm_client.py`, `carrac/ai/summarizer.py`, and other callers.
"""

from __future__ import annotations

import logging
import os

from carrac.llm.anthropic import AnthropicProvider

logger = logging.getLogger('carrac.ai.llm')


def _current_provider() -> AnthropicProvider:
    """One provider instance per-model. Picks up `AI_MODEL` from env."""
    model = os.getenv('AI_MODEL', 'claude-opus-4-6')
    return AnthropicProvider(model=model)


def stream_chat(messages, max_tokens=2000):
    """Stream text from Anthropic. Legacy two-tuple shape:
        (text_chunk, None) per chunk
        (None, usage_dict) at end
    Raises RuntimeError on failure (LLMError is a RuntimeError subclass).
    """
    provider = _current_provider()
    for event_type, payload, meta in provider.stream_chat(messages, max_tokens=max_tokens):
        if event_type == 'text':
            yield (payload, None)
        elif event_type == 'usage':
            # Legacy callers expect prompt_tokens/completion_tokens keys.
            usage = dict(meta or {})
            if 'prompt_tokens' not in usage and 'input_tokens' in usage:
                usage['prompt_tokens'] = usage['input_tokens']
            if 'completion_tokens' not in usage and 'output_tokens' in usage:
                usage['completion_tokens'] = usage['output_tokens']
            yield (None, usage)


def stream_chat_with_tools(messages, tools=None, max_tokens=2000):
    """Stream with tool calling. Legacy three-tuple shape."""
    provider = _current_provider()
    for event in provider.stream_chat(messages, tools=tools, max_tokens=max_tokens):
        event_type, payload, meta = event
        if event_type == 'text':
            yield ('text', payload, None)
        elif event_type == 'tool_call':
            yield ('tool_call', None, meta)
        elif event_type == 'usage':
            usage = dict(meta or {})
            if 'prompt_tokens' not in usage and 'input_tokens' in usage:
                usage['prompt_tokens'] = usage['input_tokens']
            if 'completion_tokens' not in usage and 'output_tokens' in usage:
                usage['completion_tokens'] = usage['output_tokens']
            yield ('usage', None, usage)


def complete_chat(messages, max_tokens=500, model=None):
    """Non-streaming completion for summarization."""
    provider = _current_provider()
    text, usage = provider.complete_chat(messages, max_tokens=max_tokens, model=model)
    return text, usage


# ---------------------------------------------------------------------------
# Async wrappers — used by carrac/ai/llm_client.py async dispatchers.
# ---------------------------------------------------------------------------

async def astream_chat(messages, max_tokens=2000):
    """Async stream of text from Anthropic. Legacy two-tuple shape:
        (text_chunk, None) per chunk
        (None, usage_dict) at end
    Raises LLMError on failure.
    """
    provider = _current_provider()
    async for event_type, payload, meta in provider.astream_chat(messages, max_tokens=max_tokens):
        if event_type == 'text':
            yield (payload, None)
        elif event_type == 'usage':
            usage = dict(meta or {})
            if 'prompt_tokens' not in usage and 'input_tokens' in usage:
                usage['prompt_tokens'] = usage['input_tokens']
            if 'completion_tokens' not in usage and 'output_tokens' in usage:
                usage['completion_tokens'] = usage['output_tokens']
            yield (None, usage)


async def astream_chat_with_tools(messages, tools=None, max_tokens=2000):
    """Async stream with tool calling. Legacy three-tuple shape:
        ('text', payload, None)
        ('tool_call', None, meta)
        ('usage', None, usage_dict)
    """
    provider = _current_provider()
    async for event_type, payload, meta in provider.astream_chat(
        messages, tools=tools, max_tokens=max_tokens
    ):
        if event_type == 'text':
            yield ('text', payload, None)
        elif event_type == 'tool_call':
            yield ('tool_call', None, meta)
        elif event_type == 'usage':
            usage = dict(meta or {})
            if 'prompt_tokens' not in usage and 'input_tokens' in usage:
                usage['prompt_tokens'] = usage['input_tokens']
            if 'completion_tokens' not in usage and 'output_tokens' in usage:
                usage['completion_tokens'] = usage['output_tokens']
            yield ('usage', None, usage)
