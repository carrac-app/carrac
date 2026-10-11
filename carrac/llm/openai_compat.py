"""
OpenAICompatProvider — raw requests + SSE for OpenAI-compatible backends.

Handles xAI Grok chat-completions (the existing `carrac/ai/kimi_client.py`
surface), Together.ai, etc. Used by the web streaming dispatcher.

Cost tracking for this class is minimal — the web surface doesn't need
cumulative tracking; `carrac/ai/billing.py` handles per-request billing.
"""

from __future__ import annotations

import logging
import os
from typing import Iterator, Optional

import requests

from carrac.llm.base import LLMError, LLMProvider, StreamEvent

logger = logging.getLogger('carrac.llm.openai_compat')


class OpenAICompatProvider(LLMProvider):
    """Generic OpenAI-compatible streaming client (xAI, Together, etc.)."""

    def __init__(self, *,
                 model: Optional[str] = None,
                 api_key: Optional[str] = None,
                 base_url: Optional[str] = None):
        self._model_default = model
        self._api_key_override = api_key
        self._base_url_override = base_url

    @property
    def provider_name(self) -> str:
        return 'openai_compat'

    @property
    def model(self) -> str:
        cfg_model = os.getenv('AI_MODEL', '') or os.getenv('TOGETHER_MODEL', '')
        return cfg_model or self._model_default or ''

    @property
    def _api_key(self) -> str:
        if self._api_key_override:
            return self._api_key_override
        return os.getenv('AI_API_KEY', '') or os.getenv('TOGETHER_API_KEY', '')

    @property
    def _base_url(self) -> str:
        if self._base_url_override:
            return self._base_url_override
        # Lazy import mirrors carrac.integrations._init_posthog_config. Still
        # reads os.getenv directly (not the frozen Config class attribute) so
        # a per-process env override — e.g. tests monkeypatching AI_BASE_URL
        # after Config has already been imported — is honored dynamically;
        # only the *default* literal is single-sourced from Config.AI_BASE_URL
        # instead of being re-declared (and drifting) here.
        from carrac.config import Config
        return os.getenv('AI_BASE_URL', Config.AI_BASE_URL)

    # ---------------------------------------------------------- streaming

    def stream_chat(self, messages: list, *,
                    tools: Optional[list] = None,
                    max_tokens: int = 2000,
                    temperature: Optional[float] = None) -> Iterator[StreamEvent]:
        """Stream OpenAI-compatible SSE, yielding canonical events."""
        api_key = self._api_key
        model = self.model
        if not api_key or not model:
            raise LLMError("AI service not configured")

        body: dict = {
            'model': model,
            'messages': messages,
            'max_tokens': max_tokens,
            'stream': True,
            'stream_options': {'include_usage': True},
        }
        if tools:
            body['tools'] = tools
        if temperature is not None:
            body['temperature'] = temperature

        try:
            resp = requests.post(
                self._base_url,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                },
                json=body,
                stream=True,
                timeout=(10, 300),
            )
        except requests.RequestException as e:
            logger.error(f"LLM connection error: {e}")
            raise LLMError("AI service unavailable")

        if resp.status_code != 200:
            logger.error(f"LLM API {resp.status_code}: {resp.text[:200]}")
            raise LLMError("AI service unavailable")

        usage: Optional[dict] = None
        tool_calls: dict = {}  # idx -> {id, name, arguments_str}

        for chunk in self._iter_sse(resp):
            if chunk.get('usage'):
                usage = self._normalize_openai_reasoning_tokens(chunk['usage'])

            choices = chunk.get('choices', [])
            if not choices:
                continue

            delta = choices[0].get('delta', {})

            content = delta.get('content')
            if content:
                yield ('text', content, None)

            for tc in delta.get('tool_calls', []):
                idx = tc.get('index', 0)
                if idx not in tool_calls:
                    tool_calls[idx] = {
                        'id': tc.get('id', ''),
                        'name': (tc.get('function') or {}).get('name', ''),
                        'arguments_str': '',
                    }
                entry = tool_calls[idx]
                if tc.get('id'):
                    entry['id'] = tc['id']
                fn = tc.get('function') or {}
                if fn.get('name'):
                    entry['name'] = fn['name']
                if fn.get('arguments'):
                    entry['arguments_str'] += fn['arguments']

            finish = choices[0].get('finish_reason')
            if finish == 'tool_calls':
                import json as _json
                for idx in sorted(tool_calls.keys()):
                    entry = tool_calls[idx]
                    try:
                        args = _json.loads(entry['arguments_str']) if entry['arguments_str'] else {}
                    except _json.JSONDecodeError:
                        args = {}
                    yield ('tool_call', None, {
                        'id': entry['id'],
                        'name': entry['name'],
                        'arguments': args,
                    })
                tool_calls.clear()

        yield ('usage', None, usage or {})

    # ---------------------------------------------------------------- cost

    def calculate_cost(self, usage: dict) -> float:
        """Web surface delegates cost to `carrac/ai/billing.py` — return 0 here."""
        return 0.0
