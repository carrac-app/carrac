"""
LLM client dispatcher — routes to the correct provider based on config.

Provides a unified stream_chat() / astream_chat() interface regardless of backend.
Provider selection: AI_PROVIDER env var, or auto-detected from model name.

Async dispatchers (astream_chat / astream_chat_with_tools):
- For 'anthropic': delegates to AnthropicProvider.astream_chat() which uses
  anthropic.AsyncAnthropic — fully non-blocking.
- For 'openai' / kimi: wraps the sync iterator per-chunk via asyncio.to_thread()
  so the event loop is not blocked. This is best-effort (kimi has no async path);
  native async can be added if kimi usage becomes significant.
"""

import asyncio
import logging
import os

logger = logging.getLogger('carrac.ai.llm')


def stream_chat(messages, max_tokens=2000):
    """Stream a chat completion from the configured LLM provider.

    Auto-detects provider from AI_PROVIDER config or model name prefix.

    Yields:
        (str, None) -- content chunk
        (None, dict) -- final usage stats {prompt_tokens, completion_tokens}
    Raises:
        RuntimeError on API/network errors.
    """
    provider = _get_provider()

    if provider == 'anthropic':
        from carrac.ai.anthropic_client import stream_chat as _stream
    else:
        from carrac.ai.kimi_client import stream_chat as _stream

    yield from _stream(messages, max_tokens)


def stream_chat_with_tools(messages, tools=None, max_tokens=2000):
    """Stream a chat completion with optional tool calling support.

    Yields tuples of (event_type, chunk, meta):
        ('text', str, None)        — text content chunk
        ('tool_call', None, dict)  — LLM wants to call a tool {id, name, arguments}
        ('usage', None, dict)      — final usage stats
    """
    provider = _get_provider()

    if provider == 'anthropic':
        from carrac.ai.anthropic_client import stream_chat_with_tools as _stream
    else:
        from carrac.ai.kimi_client import stream_chat_with_tools as _stream

    yield from _stream(messages, tools, max_tokens)


async def _async_iter_sync(sync_gen):
    """Wrap a synchronous generator to yield items asynchronously via asyncio.to_thread.

    Needed for providers (kimi/openai) that only have a sync streaming path.
    Each ``next()`` call is offloaded to a thread pool so the event loop is not
    blocked between chunks.

    StopIteration from the underlying iterator terminates the async generator.
    """
    sentinel = object()
    while True:
        item = await asyncio.to_thread(_safe_next, sync_gen, sentinel)
        if item is sentinel:
            return
        yield item


def _safe_next(gen, sentinel):
    """Call next(gen), return sentinel on StopIteration."""
    try:
        return next(gen)
    except StopIteration:
        return sentinel


async def astream_chat(messages, max_tokens=2000):
    """Async version of stream_chat — yields (text_chunk, None) and (None, usage).

    For 'anthropic': uses AsyncAnthropic (fully non-blocking).
    For 'openai'/kimi: wraps the sync stream via asyncio.to_thread (best-effort).
    """
    provider = _get_provider()

    if provider == 'anthropic':
        from carrac.ai.anthropic_client import astream_chat as _astream
        async for item in _astream(messages, max_tokens):
            yield item
    else:
        from carrac.ai.kimi_client import stream_chat as _sync_stream
        sync_gen = _sync_stream(messages, max_tokens)
        async for item in _async_iter_sync(sync_gen):
            yield item


async def astream_chat_with_tools(messages, tools=None, max_tokens=2000):
    """Async version of stream_chat_with_tools.

    Yields:
        ('text', str, None)        — text content chunk
        ('tool_call', None, dict)  — LLM wants to call a tool {id, name, arguments}
        ('usage', None, dict)      — final usage stats

    For 'anthropic': uses AsyncAnthropic (fully non-blocking).
    For 'openai'/kimi: wraps the sync stream via asyncio.to_thread (best-effort).
    """
    provider = _get_provider()

    if provider == 'anthropic':
        from carrac.ai.anthropic_client import astream_chat_with_tools as _astream
        async for item in _astream(messages, tools, max_tokens):
            yield item
    else:
        from carrac.ai.kimi_client import stream_chat_with_tools as _sync_stream
        sync_gen = _sync_stream(messages, tools, max_tokens)
        async for item in _async_iter_sync(sync_gen):
            yield item


def _get_provider():
    """Detect LLM provider from env."""
    explicit = os.getenv('AI_PROVIDER', '').lower()
    if explicit:
        return explicit

    # Auto-detect from model name
    model = os.getenv('AI_MODEL', '')
    if model.startswith('claude-'):
        return 'anthropic'

    return 'openai'
