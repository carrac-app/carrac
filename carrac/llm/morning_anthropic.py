"""
MorningAnthropicProvider — raw HTTP + SSE Anthropic client for morning engine.

Diverges from ``AnthropicProvider`` (carrac/llm/anthropic.py) in that it
does NOT use the anthropic SDK. The morning engine's 4:45 AM run needs:
  - Explicit (connect, read) timeout tuple — Opus prefill on large prompts
    can exceed 300 s before the first streaming chunk.
  - Independent API-key / model source (env vars + config, not Flask app).
  - Thread-safe cumulative cost tracking (via TrackedLLMProvider).

The ``chat()`` method returns the legacy dict shape the morning engine
depends on: ``{'content', 'usage', 'elapsed'}`` with cumulative totals
updated on each call.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Iterator, Optional

import requests

from carrac.ai.pricing import ANTHROPIC_RATES, ANTHROPIC_DEFAULT
from carrac.llm.base import LLMError, LLMProvider, StreamEvent, TrackedLLMProvider

log = logging.getLogger('morning_engine.anthropic')


def _rates_for(model: str) -> dict:
    return ANTHROPIC_RATES.get(model) or ANTHROPIC_RATES[ANTHROPIC_DEFAULT]


class MorningAnthropicProvider(TrackedLLMProvider, LLMProvider):
    """Anthropic client for the morning engine (raw HTTP + SSE)."""

    def __init__(self, api_key: str, model: str):
        super().__init__()
        self._api_key = api_key
        self._model = model
        rates = _rates_for(model)
        self._rates_per_tok = {
            'input': rates['input'] / 1e6,
            'output': rates['output'] / 1e6,
            'cache_read': rates['cache_read'] / 1e6,
            'cache_write': rates['cache_write'] / 1e6,
        }
        self._model_label = (
            'Sonnet' if 'sonnet' in model
            else 'Opus' if 'opus' in model
            else 'Haiku' if 'haiku' in model
            else 'Anthropic'
        )
        log.info(
            "Anthropic client: %s @ $%.0f/$%.0f/MTok",
            model, self._rates_per_tok['input'] * 1e6, self._rates_per_tok['output'] * 1e6,
        )
        self.session = requests.Session()
        self.session.headers.update({
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        })

    @property
    def provider_name(self) -> str:
        return 'anthropic'

    @property
    def model(self) -> str:
        return self._model

    # ---------------------------------------------------------------- cost

    def calculate_cost(self, usage: dict) -> float:
        inp = int(usage.get('input_tokens', 0) or 0)
        out = int(usage.get('output_tokens', 0) or 0)
        cache_read = int(usage.get('cache_read_input_tokens', usage.get('cache_read_tokens', 0)) or 0)
        cache_write = int(usage.get('cache_creation_input_tokens', usage.get('cache_write_tokens', 0)) or 0)
        standard_inp = max(0, inp - cache_read - cache_write)
        return (
            standard_inp * self._rates_per_tok['input']
            + cache_read * self._rates_per_tok['cache_read']
            + cache_write * self._rates_per_tok['cache_write']
            + out * self._rates_per_tok['output']
        )

    # ---------------------------------------------------------- streaming

    def stream_chat(self, messages: list, *,
                    tools: Optional[list] = None,
                    max_tokens: int = 16000,
                    temperature: Optional[float] = 0.7) -> Iterator[StreamEvent]:
        """Stream via Anthropic Messages API (SSE). Canonical event shape."""
        system_blocks, conv_messages = self._split_messages(messages)
        payload: dict = {
            'model': self._model,
            'system': system_blocks,
            'messages': conv_messages,
            'temperature': temperature if temperature is not None else 0.7,
            'max_tokens': max_tokens,
            'stream': True,
        }

        try:
            resp = self.session.post(
                'https://api.anthropic.com/v1/messages',
                json=payload,
                timeout=(30, 300),  # connect 30s / read 300s (caller can override via chat())
                stream=True,
            )
        except requests.RequestException as e:
            log.error("Anthropic connection error: %s", e)
            raise LLMError(f"Anthropic API connection error: {e}")

        if resp.status_code != 200:
            body = resp.text[:500]
            log.error("Anthropic API error %d: %s", resp.status_code, body)
            raise LLMError(f"Anthropic API error {resp.status_code}: {body[:200]}")

        usage_data: dict = {}
        try:
            for event in self._iter_sse(resp):
                etype = event.get('type')
                if etype == 'error':
                    err = event.get('error', {}) or {}
                    err_type = err.get('type', 'unknown_error')
                    err_msg = err.get('message', '')
                    log.error("Anthropic SSE error: %s — %s", err_type, err_msg)
                    raise LLMError(f"Anthropic API error ({err_type}): {err_msg}")
                elif etype == 'message_start':
                    msg_usage = event.get('message', {}).get('usage', {})
                    usage_data['input_tokens'] = msg_usage.get('input_tokens', 0)
                    usage_data['cache_read_input_tokens'] = msg_usage.get('cache_read_input_tokens', 0)
                    usage_data['cache_creation_input_tokens'] = msg_usage.get('cache_creation_input_tokens', 0)
                elif etype == 'content_block_delta':
                    delta = event.get('delta', {})
                    if delta.get('type') == 'text_delta':
                        yield ('text', delta.get('text', ''), None)
                elif etype == 'message_delta':
                    delta_usage = event.get('usage', {})
                    usage_data['output_tokens'] = delta_usage.get('output_tokens', 0)
        finally:
            resp.close()

        yield ('usage', None, usage_data)

    # ------------------------------------------------------- chat() override

    def chat(self, system_prompt: str, user_message: str, *,
             images: Optional[list] = None,
             max_tokens: int = 16000,
             temperature: float = 0.7,
             timeout: int = 300) -> dict:
        """Collect stream → dict with cumulative tracking. Preserves legacy shape."""
        # Build messages with provider-specific image encoding
        user_content = self._build_user_content(user_message, images=images)
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_content},
        ]

        t0 = time.time()
        text_parts: list[str] = []
        usage_data: dict = {}

        # Re-issue the request with caller's read timeout (default 300s)
        # by driving stream_chat with a custom session timeout. The SSE parse
        # logic is the same, so we inline it here rather than plumb timeout
        # through stream_chat's signature.
        system_blocks, conv_messages = self._split_messages(messages)
        payload = {
            'model': self._model,
            'system': system_blocks,
            'messages': conv_messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
            'stream': True,
        }

        try:
            resp = self.session.post(
                'https://api.anthropic.com/v1/messages',
                json=payload,
                timeout=(30, timeout),
                stream=True,
            )
        except requests.RequestException as e:
            log.error("Anthropic connection error: %s", e)
            raise LLMError(f"Anthropic API connection error: {e}")

        if resp.status_code != 200:
            body = resp.text[:500]
            log.error("Anthropic API error %d: %s", resp.status_code, body)
            raise LLMError(f"Anthropic API error {resp.status_code}: {body[:200]}")

        stop_reason = None
        try:
            for event in self._iter_sse(resp):
                etype = event.get('type')
                if etype == 'error':
                    err = event.get('error', {}) or {}
                    err_type = err.get('type', 'unknown_error')
                    err_msg = err.get('message', '')
                    log.error("Anthropic SSE error: %s — %s", err_type, err_msg)
                    raise LLMError(f"Anthropic API error ({err_type}): {err_msg}")
                elif etype == 'message_start':
                    msg_usage = event.get('message', {}).get('usage', {})
                    usage_data['input_tokens'] = msg_usage.get('input_tokens', 0)
                    usage_data['cache_read_input_tokens'] = msg_usage.get('cache_read_input_tokens', 0)
                    usage_data['cache_creation_input_tokens'] = msg_usage.get('cache_creation_input_tokens', 0)
                elif etype == 'content_block_delta':
                    delta = event.get('delta', {})
                    if delta.get('type') == 'text_delta':
                        text_parts.append(delta.get('text', ''))
                elif etype == 'message_delta':
                    delta_usage = event.get('usage', {})
                    usage_data['output_tokens'] = delta_usage.get('output_tokens', 0)
                    _sr = (event.get('delta', {}) or {}).get('stop_reason')
                    if _sr:
                        stop_reason = _sr
        finally:
            resp.close()

        elapsed = time.time() - t0
        text = ''.join(text_parts)

        # Compute cost + track in cumulative totals
        cost = self.calculate_cost(usage_data)
        inp = int(usage_data.get('input_tokens', 0) or 0)
        out = int(usage_data.get('output_tokens', 0) or 0)
        cache_read = int(usage_data.get('cache_read_input_tokens', 0) or 0)

        usage_result = {
            'input_tokens': inp,
            'output_tokens': out,
            'cache_read_tokens': cache_read,
            'cost': cost,
        }
        self._track(usage_result)

        log.info(
            "%s chat: %d in (%d cached) / %d out, $%.4f, %.1fs",
            self._model_label, inp, cache_read, out, cost, elapsed,
        )

        if stop_reason == 'max_tokens':
            log.warning(
                "%s chat hit max_tokens (%d) — response truncated; tail content lost",
                self._model_label, max_tokens,
            )
        return {'content': text, 'usage': usage_result, 'elapsed': elapsed,
                'stop_reason': stop_reason}

    # -------------------------------------------------- internal helpers

    @staticmethod
    def _split_messages(messages: list):
        """Anthropic-style system blocks + conversation messages with cache_control."""
        system_text = ''
        conv_messages = []
        for msg in messages:
            if msg['role'] == 'system':
                system_text = msg['content']
            else:
                conv_messages.append({'role': msg['role'], 'content': msg['content']})
        system_blocks = [
            {'type': 'text', 'text': system_text, 'cache_control': {'type': 'ephemeral'}}
        ] if system_text else []
        return system_blocks, conv_messages

    def _build_user_content(self, user_message: str, *,
                            images: Optional[list] = None) -> Any:
        if not images:
            return user_message
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
