"""
LLMProvider ABC — unified chat + streaming surface.

Canonical event tuple yielded by ``stream_chat()``:

    ('text', str, None)                    # text token
    ('tool_call', None, {'id', 'name', 'arguments': dict})
    ('usage', None, {'input_tokens', 'output_tokens', ...})

Subclasses:
  - ``AnthropicProvider`` (uses `anthropic` SDK, cache_control)
  - ``OpenAICompatProvider`` (raw requests + SSE, for xAI Grok chat-completions)
  - ``MorningAnthropicProvider`` (raw requests + SSE, for morning engine)
  - ``GrokProvider`` (raw requests, also hosts search/tts)

``TrackedLLMProvider`` is a mixin that adds thread-safe cumulative
cost/usage accounting (used by morning_engine).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator, Optional

logger = logging.getLogger('carrac.llm')


# Canonical stream event — (event_type, payload, meta)
StreamEvent = tuple[str, Any, Optional[dict]]


class LLMError(RuntimeError):
    """Wrapper raised when a provider call fails (network / API / parse)."""


@dataclass
class Usage:
    """Canonical usage breakdown (token counts + USD cost)."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> 'Usage':
        return cls(
            input_tokens=int(d.get('input_tokens', d.get('prompt_tokens', 0)) or 0),
            output_tokens=int(d.get('output_tokens', d.get('completion_tokens', 0)) or 0),
            cache_read_tokens=int(d.get('cache_read_input_tokens', d.get('cache_read_tokens', 0)) or 0),
            cache_write_tokens=int(d.get('cache_creation_input_tokens', d.get('cache_write_tokens', 0)) or 0),
            reasoning_tokens=int(d.get('reasoning_tokens', 0) or 0),
            cost=float(d.get('cost', 0.0) or 0.0),
        )


class LLMProvider(ABC):
    """Abstract LLM provider — streaming + blocking chat."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """'anthropic', 'xai', 'openai', etc."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Current model ID (e.g. 'claude-opus-4-6', 'grok-4-fast')."""

    # ------------------------------------------------------------ streaming

    @abstractmethod
    def stream_chat(self, messages: list, *,
                    tools: Optional[list] = None,
                    max_tokens: int = 2000,
                    temperature: Optional[float] = None) -> Iterator[StreamEvent]:
        """Yield canonical events. See module docstring for event shapes.

        Raises LLMError on network / API failure.
        """

    # ---------------------------------------------------------------- cost

    @abstractmethod
    def calculate_cost(self, usage: dict) -> float:
        """USD cost for the given usage dict."""

    # ------------------------------------------------------ blocking chat

    def chat(self, system_prompt: str, user_message: str, *,
             images: Optional[list] = None,
             max_tokens: int = 16000,
             temperature: float = 0.7,
             timeout: int = 300) -> dict:
        """Build messages + consume stream. Returns {'content', 'usage', 'elapsed', 'cost'}.

        Subclasses may override to use a native non-streaming API (e.g. Grok Chat
        Completions is blocking end-to-end, no need to stream-and-collect).
        """
        messages = self._build_messages(system_prompt, user_message, images=images)

        t0 = time.time()
        text_parts: list[str] = []
        usage_dict: dict = {}

        for event_type, payload, meta in self.stream_chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            if event_type == 'text':
                text_parts.append(payload)
            elif event_type == 'usage':
                usage_dict = meta or {}

        elapsed = time.time() - t0
        cost = self.calculate_cost(usage_dict)

        # Preserve legacy dict shape — keep existing keys callers rely on.
        result_usage = dict(usage_dict)
        result_usage.setdefault('input_tokens', usage_dict.get('input_tokens', 0))
        result_usage.setdefault('output_tokens', usage_dict.get('output_tokens', 0))
        result_usage['cost'] = cost

        return {
            'content': ''.join(text_parts),
            'usage': result_usage,
            'elapsed': elapsed,
        }

    # ----------------------------------------------- provider-specific hooks

    def _build_messages(self, system_prompt: str, user_message: str, *,
                        images: Optional[list] = None) -> list:
        """Build provider-neutral messages list. Subclasses override for images
        because encoding differs (Anthropic base64 block vs OpenAI data URL).
        """
        user_content = self._build_user_content(user_message, images=images)
        return [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_content},
        ]

    def _build_user_content(self, user_message: str, *,
                            images: Optional[list] = None) -> Any:
        """Default: plain text. Override to inject images with provider-specific encoding."""
        if images:
            raise NotImplementedError(
                f"{type(self).__name__}._build_user_content must handle images"
            )
        return user_message

    # -------------------------------------------------- shared SSE helpers

    @staticmethod
    def _iter_sse(response, *, encoding: str = 'utf-8') -> Iterator[dict]:
        """Iterate decoded JSON events from a `data:` SSE stream.

        Yields parsed JSON dicts. Stops on `data: [DONE]`. Silently skips
        non-JSON lines. Sets ``response.encoding`` — some SSE APIs omit the
        charset header and `requests` otherwise falls back to ISO-8859-1.
        """
        response.encoding = encoding
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith('data: '):
                continue
            payload = raw_line[6:]
            if payload == '[DONE]':
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue

    @staticmethod
    def _normalize_openai_reasoning_tokens(usage: dict) -> dict:
        """xAI Grok: reasoning_tokens are billed as output but not always in
        completion_tokens. Adjust in-place and return the dict.
        """
        if not usage:
            return usage
        details = usage.get('completion_tokens_details', {}) or {}
        reasoning = details.get('reasoning_tokens', 0) or 0
        if reasoning and usage.get('completion_tokens', 0) < reasoning:
            usage['completion_tokens'] = usage.get('completion_tokens', 0) + reasoning
        return usage

    # ------------------------------------------------- factory

    @classmethod
    def for_model(cls, model: str, **kwargs) -> 'LLMProvider':
        """Pick the right subclass by model name.

        - ``claude-*`` → ``AnthropicProvider`` (uses anthropic SDK, for web surface)
        - anything else → ``OpenAICompatProvider`` (raw SSE, for web surface)

        Morning engine has its own factory in ``morning_engine.engine._create_primary_client``.
        """
        if model.startswith('claude-'):
            from carrac.llm.anthropic import AnthropicProvider
            return AnthropicProvider(model=model, **kwargs)
        from carrac.llm.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(model=model, **kwargs)


class TrackedLLMProvider:
    """Mixin: thread-safe cumulative usage + cost accounting.

    Wraps whatever `chat()` the subclass exposes so each call updates totals.
    Morning engine uses this; web clients do not (billing lives in
    `carrac/ai/billing.py`).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._usage_lock = threading.Lock()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cost = 0.0

    def _track(self, usage: dict) -> dict:
        """Merge a usage dict into cumulative totals. Returns the same dict."""
        inp = int(usage.get('input_tokens', 0) or 0)
        out = int(usage.get('output_tokens', 0) or 0)
        cache_read = int(usage.get('cache_read_tokens', 0) or 0)
        cost = float(usage.get('cost', 0.0) or 0.0)
        with self._usage_lock:
            self.total_input_tokens += inp
            self.total_output_tokens += out
            self.total_cache_read_tokens += cache_read
            self.total_cost += cost
        return usage

    def get_usage_summary(self) -> dict:
        """Cumulative totals since construction."""
        with self._usage_lock:
            return {
                'total_input_tokens': self.total_input_tokens,
                'total_output_tokens': self.total_output_tokens,
                'total_cache_read_tokens': self.total_cache_read_tokens,
                'total_cost': round(self.total_cost, 4),
            }
