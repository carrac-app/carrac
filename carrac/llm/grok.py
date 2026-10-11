"""
GrokProvider — xAI Grok for morning engine (blocking chat + search + tts).

Morning engine uses Grok Chat Completions (blocking, not streaming) for
report generation, plus the Responses API (with web_search / x_search tools)
for news sweeps, plus TTS for audio briefings.

``stream_chat()`` implements the canonical SSE streaming shape so this class
plays nicely with the unified interface, but the morning engine's
``chat()`` override keeps the current blocking HTTP pattern byte-for-byte.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Iterator, Optional

import requests

from carrac.ai.pricing import GROK_RATES
from carrac.llm.base import LLMError, LLMProvider, StreamEvent, TrackedLLMProvider

log = logging.getLogger('morning_engine.grok')


class GrokProvider(TrackedLLMProvider, LLMProvider):
    """xAI Grok client — blocking chat + responses + TTS + SSE streaming."""

    def __init__(self, api_key: str, base_url: str, model: str):
        super().__init__()
        self._api_key = api_key
        self._base_url = base_url.rstrip('/')
        self._model = model
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        })

    @property
    def provider_name(self) -> str:
        return 'xai'

    @property
    def model(self) -> str:
        return self._model

    # ---------------------------------------------------------------- cost

    def calculate_cost(self, usage: dict) -> float:
        inp = int(usage.get('input_tokens', usage.get('prompt_tokens', 0)) or 0)
        out = int(usage.get('output_tokens', usage.get('completion_tokens', 0)) or 0)
        return (
            inp * GROK_RATES['chat']['input'] / 1_000_000
            + out * GROK_RATES['chat']['output'] / 1_000_000
        )

    # ---------------------------------------------------------- streaming

    def stream_chat(self, messages: list, *,
                    tools: Optional[list] = None,
                    max_tokens: int = 2000,
                    temperature: Optional[float] = None) -> Iterator[StreamEvent]:
        """OpenAI-compat SSE streaming via xAI chat-completions endpoint."""
        body: dict = {
            'model': self._model,
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
            resp = self.session.post(
                f'{self._base_url}/chat/completions',
                json=body,
                stream=True,
                timeout=(10, 300),
            )
        except requests.RequestException as e:
            log.error("Grok connection error: %s", e)
            raise LLMError("Grok service unavailable")

        if resp.status_code != 200:
            log.error("Grok API %d: %s", resp.status_code, resp.text[:200])
            raise LLMError(f"Grok API error {resp.status_code}")

        usage: Optional[dict] = None
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

        yield ('usage', None, usage or {})

    # ---------------------------------------------------- blocking chat()

    def chat(self, system_prompt: str, user_message: str, *,
             images: Optional[list] = None,
             max_tokens: int = 16000,
             temperature: float = 0.7,
             timeout: int = 300) -> dict:
        """Blocking chat-completions (no SSE). Preserves legacy morning engine behaviour."""
        user_content = self._build_user_content(user_message, images=images)
        payload: dict = {
            'model': self._model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_content},
            ],
            'temperature': temperature,
            'max_tokens': max_tokens,
        }

        t0 = time.time()
        resp = self.session.post(
            f'{self._base_url}/chat/completions',
            json=payload,
            timeout=timeout,
        )
        elapsed = time.time() - t0

        if resp.status_code != 200:
            log.error("Chat API error %d: %s", resp.status_code, resp.text[:500])
            raise LLMError(f"Grok chat API error {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        text = data['choices'][0]['message']['content']
        raw_usage = data.get('usage', {}) or {}

        inp = int(raw_usage.get('prompt_tokens', raw_usage.get('input_tokens', 0)) or 0)
        out = int(raw_usage.get('completion_tokens', raw_usage.get('output_tokens', 0)) or 0)
        cost = self.calculate_cost({'input_tokens': inp, 'output_tokens': out})

        usage_result = {'input_tokens': inp, 'output_tokens': out, 'cost': cost}
        self._track(usage_result)

        log.info(
            "Chat: %d in / %d out, $%.4f, %.1fs",
            inp, out, cost, elapsed,
        )

        return {'content': text, 'usage': usage_result, 'elapsed': elapsed}

    # -------------------------------------------------- Responses API (search)

    def search(self, query: str, tools: Optional[list] = None,
               max_tokens: int = 4000, temperature: float = 0.5,
               timeout: int = 120) -> dict:
        """Grok Responses API with web_search / x_search tools.

        Returns: ``{'content', 'citations', 'usage', 'elapsed'}``.
        """
        if tools is None:
            tools = [{'type': 'web_search'}]

        payload = {
            'model': self._model,
            'input': [{'role': 'user', 'content': query}],
            'tools': tools,
            'temperature': temperature,
            'max_output_tokens': max_tokens,
        }

        t0 = time.time()
        resp = self.session.post(
            f'{self._base_url}/responses',
            json=payload,
            timeout=timeout,
        )
        elapsed = time.time() - t0

        if resp.status_code != 200:
            log.error("Responses API error %d: %s", resp.status_code, resp.text[:500])
            raise LLMError(f"Grok responses API error {resp.status_code}: {resp.text[:200]}")

        data = resp.json()

        output_text = ''
        citations: list = []
        for item in data.get('output', []):
            if item.get('type') == 'message':
                for block in item.get('content', []):
                    if block.get('type') == 'output_text':
                        output_text += block.get('text', '')
                        for ann in block.get('annotations', []):
                            if ann.get('type') == 'url_citation':
                                citations.append(ann.get('url', ''))

        raw_usage = data.get('usage', {}) or {}
        inp = int(raw_usage.get('prompt_tokens', raw_usage.get('input_tokens', 0)) or 0)
        out = int(raw_usage.get('completion_tokens', raw_usage.get('output_tokens', 0)) or 0)
        cost = self.calculate_cost({'input_tokens': inp, 'output_tokens': out})

        usage_result = {'input_tokens': inp, 'output_tokens': out, 'cost': cost}
        self._track(usage_result)

        log.info(
            "Search: %d in / %d out, $%.4f, %.1fs, %d citations",
            inp, out, cost, elapsed, len(citations),
        )

        return {
            'content': output_text,
            'citations': citations,
            'usage': usage_result,
            'elapsed': elapsed,
        }

    # ---------------------------------------------------------------- TTS

    def tts(self, text: str, voice_id: str = 'rex', codec: str = 'mp3',
            sample_rate: int = 24000, timeout: int = 60) -> bytes:
        """Generate TTS audio. Returns raw audio bytes.

        4096-char hard limit — truncates at last sentence boundary before 4000.
        """
        if len(text) > 4096:
            original_len = len(text)
            truncated = text[:4000]
            last_period = truncated.rfind('.')
            if last_period > 3000:
                text = truncated[:last_period + 1]
            else:
                text = truncated
            log.warning("TTS text truncated from %d to %d chars", original_len, len(text))

        payload = {
            'text': text,
            'voice_id': voice_id,
            'language': 'en',
            'output_format': {'codec': codec, 'sample_rate': sample_rate},
        }

        t0 = time.time()
        resp = self.session.post(
            f'{self._base_url}/tts',
            json=payload,
            timeout=timeout,
        )
        elapsed = time.time() - t0

        if resp.status_code != 200:
            log.error("TTS API error %d: %s", resp.status_code, resp.text[:500])
            raise LLMError(f"Grok TTS API error {resp.status_code}: {resp.text[:200]}")

        char_cost = len(text) * GROK_RATES['tts']['per_1m_chars'] / 1_000_000
        with self._usage_lock:
            self.total_cost += char_cost

        log.info(
            "TTS: %d chars, $%.4f, %.1fs, %d bytes",
            len(text), char_cost, elapsed, len(resp.content),
        )

        return resp.content

    # ----------------------------------------------- image content encoding

    def _build_user_content(self, user_message: str, *,
                            images: Optional[list] = None) -> Any:
        if not images:
            return user_message
        content: list = []
        for img_path, mime_type in images:
            with open(img_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            content.append({
                'type': 'image_url',
                'image_url': {'url': f'data:{mime_type};base64,{b64}'},
            })
        content.append({'type': 'text', 'text': user_message})
        return content
