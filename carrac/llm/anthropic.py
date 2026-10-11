"""
AnthropicProvider — web surface using the official `anthropic` SDK.

Used by `carrac/ai/anthropic_client.py` thin wrappers (stream_chat,
stream_chat_with_tools, complete_chat). Cache_control on system prompts.

Morning engine uses ``MorningAnthropicProvider`` in `carrac/llm/morning_anthropic.py`
which uses raw HTTP because the morning 4:45 AM run needs fine-grained
timeout control and explicit SSE parsing.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, AsyncIterator, Iterator, Optional

from carrac.ai.pricing import ANTHROPIC_RATES, ANTHROPIC_DEFAULT
from carrac.llm.base import LLMError, LLMProvider, StreamEvent

logger = logging.getLogger('carrac.llm.anthropic')

# Shared SDK singleton keyed by API key (connection pooling across requests).
_sdk_client = None
_sdk_client_key: Optional[str] = None
_sdk_client_lock = threading.Lock()

# Async SDK singleton — separate from the sync one (different class).
_async_sdk_client = None
_async_sdk_client_key: Optional[str] = None
_async_sdk_client_lock = threading.Lock()


def _get_async_sdk_client():
    """Lazy-init anthropic.AsyncAnthropic client, reused across async calls.

    Uses a threading.Lock for the singleton guard because this function may
    be called from asyncio.to_thread contexts as well as async code.
    """
    global _async_sdk_client, _async_sdk_client_key
    import anthropic

    api_key = (os.environ.get('ANTHROPIC_API_KEY', '')
               or os.environ.get('AI_API_KEY', ''))
    if not api_key:
        raise LLMError("Anthropic API key not configured")

    if _async_sdk_client is not None and _async_sdk_client_key == api_key:
        return _async_sdk_client

    with _async_sdk_client_lock:
        if _async_sdk_client is None or _async_sdk_client_key != api_key:
            _async_sdk_client = anthropic.AsyncAnthropic(api_key=api_key, timeout=300.0)
            _async_sdk_client_key = api_key
    return _async_sdk_client


def _get_sdk_client():
    """Lazy-init anthropic.Anthropic client, reused across calls."""
    global _sdk_client, _sdk_client_key
    import anthropic

    api_key = (os.environ.get('ANTHROPIC_API_KEY', '')
               or os.environ.get('AI_API_KEY', ''))
    if not api_key:
        raise LLMError("Anthropic API key not configured")

    if _sdk_client is not None and _sdk_client_key == api_key:
        return _sdk_client

    with _sdk_client_lock:
        if _sdk_client is None or _sdk_client_key != api_key:
            _sdk_client = anthropic.Anthropic(api_key=api_key, timeout=300.0)
            _sdk_client_key = api_key
    return _sdk_client


def _cost_for_model(model: str, usage: dict) -> float:
    """USD cost for the given model + usage dict."""
    rates = ANTHROPIC_RATES.get(model) or ANTHROPIC_RATES[ANTHROPIC_DEFAULT]
    inp = int(usage.get('input_tokens', usage.get('prompt_tokens', 0)) or 0)
    out = int(usage.get('output_tokens', usage.get('completion_tokens', 0)) or 0)
    cache_read = int(usage.get('cache_read_input_tokens', usage.get('cache_read_tokens', 0)) or 0)
    cache_write = int(usage.get('cache_creation_input_tokens', usage.get('cache_write_tokens', 0)) or 0)
    standard_inp = max(0, inp - cache_read - cache_write)
    return (
        standard_inp * rates['input'] / 1e6
        + cache_read * rates['cache_read'] / 1e6
        + cache_write * rates['cache_write'] / 1e6
        + out * rates['output'] / 1e6
    )


class AnthropicProvider(LLMProvider):
    """Anthropic Claude via SDK — web streaming surface."""

    def __init__(self, *, model: Optional[str] = None):
        self._model = model or 'claude-opus-4-6'

    @property
    def provider_name(self) -> str:
        return 'anthropic'

    @property
    def model(self) -> str:
        return os.getenv('AI_MODEL', '') or self._model

    # ---------------------------------------------------------- streaming

    def stream_chat(self, messages: list, *,
                    tools: Optional[list] = None,
                    max_tokens: int = 2000,
                    temperature: Optional[float] = None) -> Iterator[StreamEvent]:
        """Yield canonical events from the SDK's streaming interface."""
        import anthropic as _anthropic
        client = _get_sdk_client()

        system_blocks, conv_messages = self._split_messages(messages)

        kwargs: dict = {
            'model': self.model,
            'max_tokens': max_tokens,
            'messages': conv_messages,
        }
        if system_blocks:
            kwargs['system'] = system_blocks
        if tools:
            kwargs['tools'] = tools
        if temperature is not None:
            kwargs['temperature'] = temperature

        try:
            with client.messages.stream(**kwargs) as stream:
                for event in stream:
                    etype = getattr(event, 'type', None)
                    if etype == 'content_block_delta':
                        delta = event.delta
                        if hasattr(delta, 'text'):
                            yield ('text', delta.text, None)
                message = stream.get_final_message()

            # Tool calls extracted from final message
            for block in message.content:
                if getattr(block, 'type', None) == 'tool_use':
                    yield ('tool_call', None, {
                        'id': block.id,
                        'name': block.name,
                        'arguments': block.input,
                    })

            usage = {
                'input_tokens': message.usage.input_tokens,
                'output_tokens': message.usage.output_tokens,
                'prompt_tokens': message.usage.input_tokens,
                'completion_tokens': message.usage.output_tokens,
            }
            cache_create = getattr(message.usage, 'cache_creation_input_tokens', 0) or 0
            cache_read = getattr(message.usage, 'cache_read_input_tokens', 0) or 0
            if cache_create or cache_read:
                logger.info(f"Prompt cache: created={cache_create}, read={cache_read}")
                usage['cache_creation_input_tokens'] = cache_create
                usage['cache_read_input_tokens'] = cache_read
            yield ('usage', None, usage)

        except _anthropic.APIStatusError as e:
            logger.error(f"Anthropic API error {e.status_code}: {e.message}")
            raise LLMError("AI service unavailable")
        except _anthropic.APIConnectionError as e:
            logger.error(f"Anthropic connection error: {e}")
            raise LLMError("AI service unavailable")

    def complete_chat(self, messages: list, *, max_tokens: int = 500,
                      model: Optional[str] = None) -> tuple[str, dict]:
        """Non-streaming completion (for summarization). Keeps legacy signature."""
        import anthropic as _anthropic
        client = _get_sdk_client()
        resolved_model = model or self.model

        system_blocks, conv_messages = self._split_messages(messages, cache=False)
        kwargs: dict = {
            'model': resolved_model,
            'max_tokens': max_tokens,
            'messages': conv_messages,
        }
        if system_blocks:
            kwargs['system'] = system_blocks

        try:
            response = client.messages.create(**kwargs)
            text = response.content[0].text if response.content else ''
            usage = {
                'prompt_tokens': response.usage.input_tokens,
                'completion_tokens': response.usage.output_tokens,
                'input_tokens': response.usage.input_tokens,
                'output_tokens': response.usage.output_tokens,
            }
            return text, usage
        except (_anthropic.APIStatusError, _anthropic.APIConnectionError) as e:
            logger.error(f"Anthropic API error (complete_chat): {e}")
            raise LLMError("AI service unavailable")

    # ---------------------------------------------------------------- cost

    def calculate_cost(self, usage: dict) -> float:
        return _cost_for_model(self.model, usage)

    # --------------------------------------------------- internal helpers

    @staticmethod
    def _split_messages(messages: list, *, cache: bool = True):
        """Split OpenAI-style messages into (system_blocks, conv_messages).

        Adds cache_control to the system block when `cache=True` (streaming path).
        """
        system_blocks = None
        conv_messages = []
        for msg in messages:
            if msg['role'] == 'system':
                block: dict = {'type': 'text', 'text': msg['content']}
                if cache:
                    block['cache_control'] = {'type': 'ephemeral'}
                system_blocks = [block]
            else:
                conv_messages.append({'role': msg['role'], 'content': msg['content']})
        return system_blocks, conv_messages

    # ---------------------------------------------------------------- async streaming

    async def astream_chat(self, messages: list, *,
                           tools: Optional[list] = None,
                           max_tokens: int = 2000,
                           temperature: Optional[float] = None) -> AsyncIterator[StreamEvent]:
        """Async version of stream_chat — yields the same canonical StreamEvent tuples.

        Uses anthropic.AsyncAnthropic so the event loop is not blocked.
        Intended for the FastAPI SSE path.

        Yields:
            ('text', delta_text, None)
            ('tool_call', None, {id, name, arguments})
            ('usage', None, usage_dict)
        Raises:
            LLMError on API or connection failure.
        """
        import anthropic as _anthropic
        client = _get_async_sdk_client()

        system_blocks, conv_messages = self._split_messages(messages)

        kwargs: dict = {
            'model': self.model,
            'max_tokens': max_tokens,
            'messages': conv_messages,
        }
        if system_blocks:
            kwargs['system'] = system_blocks
        if tools:
            kwargs['tools'] = tools
        if temperature is not None:
            kwargs['temperature'] = temperature

        try:
            async with client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    etype = getattr(event, 'type', None)
                    if etype == 'content_block_delta':
                        delta = event.delta
                        if hasattr(delta, 'text'):
                            yield ('text', delta.text, None)
                message = await stream.get_final_message()

            # Tool calls extracted from final message
            for block in message.content:
                if getattr(block, 'type', None) == 'tool_use':
                    yield ('tool_call', None, {
                        'id': block.id,
                        'name': block.name,
                        'arguments': block.input,
                    })

            usage = {
                'input_tokens': message.usage.input_tokens,
                'output_tokens': message.usage.output_tokens,
                'prompt_tokens': message.usage.input_tokens,
                'completion_tokens': message.usage.output_tokens,
            }
            cache_create = getattr(message.usage, 'cache_creation_input_tokens', 0) or 0
            cache_read = getattr(message.usage, 'cache_read_input_tokens', 0) or 0
            if cache_create or cache_read:
                logger.info(f"Prompt cache (async): created={cache_create}, read={cache_read}")
                usage['cache_creation_input_tokens'] = cache_create
                usage['cache_read_input_tokens'] = cache_read
            yield ('usage', None, usage)

        except _anthropic.APIStatusError as e:
            logger.error(f"Anthropic API error {e.status_code}: {e.message}")
            raise LLMError("AI service unavailable")
        except _anthropic.APIConnectionError as e:
            logger.error(f"Anthropic connection error: {e}")
            raise LLMError("AI service unavailable")

    # Provider-specific image encoding (not used by the web surface today —
    # only morning_engine uses the images= kwarg).
    def _build_user_content(self, user_message: str, *,
                            images: Optional[list] = None) -> Any:
        if not images:
            return user_message
        import base64
        content: list = []
        for img_path, mime_type in images:
            with open(img_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            content.append({
                'type': 'image',
                'source': {'type': 'base64', 'media_type': mime_type, 'data': b64},
            })
        content.append({'type': 'text', 'text': user_message})
        return content
