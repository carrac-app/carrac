"""
Unit tests for carrac.llm.base (LLMProvider ABC + TrackedLLMProvider).

Covers:
  - Canonical event stream shape
  - chat() collects text + usage from stream
  - calculate_cost() is provider-supplied (hook)
  - TrackedLLMProvider cumulative accounting is thread-safe
  - _iter_sse() parses data: frames and stops on [DONE]
  - _normalize_openai_reasoning_tokens adjusts xAI usage
"""

import threading
from unittest.mock import MagicMock


from carrac.llm.base import (
    LLMProvider,
    TrackedLLMProvider,
    Usage,
)


class FakeProvider(LLMProvider):
    """Minimal concrete provider for testing the base chat() collector."""

    def __init__(self, events=None, cost_per_call=0.01):
        self._events = events or []
        self._cost_per_call = cost_per_call

    @property
    def provider_name(self):
        return 'fake'

    @property
    def model(self):
        return 'fake-model'

    def stream_chat(self, messages, *, tools=None, max_tokens=2000, temperature=None):
        for ev in self._events:
            yield ev

    def calculate_cost(self, usage):
        return self._cost_per_call


class TrackedFakeProvider(TrackedLLMProvider, FakeProvider):
    """Mixin-first concrete class for cumulative tracking tests."""


# ---------------------------------------------------------------- Usage


def test_usage_from_dict_normalises_keys():
    u = Usage.from_dict({
        'prompt_tokens': 100,
        'completion_tokens': 50,
        'cache_read_input_tokens': 10,
        'cache_creation_input_tokens': 5,
    })
    assert u.input_tokens == 100
    assert u.output_tokens == 50
    assert u.cache_read_tokens == 10
    assert u.cache_write_tokens == 5


# ---------------------------------------------------------------- chat()


def test_chat_collects_text_and_usage():
    events = [
        ('text', 'Hello', None),
        ('text', ' world', None),
        ('usage', None, {'input_tokens': 10, 'output_tokens': 5}),
    ]
    p = FakeProvider(events=events, cost_per_call=0.05)
    r = p.chat('system prompt', 'user message')
    assert r['content'] == 'Hello world'
    assert r['usage']['input_tokens'] == 10
    assert r['usage']['output_tokens'] == 5
    assert r['usage']['cost'] == 0.05
    assert r['elapsed'] >= 0


def test_chat_ignores_tool_calls_in_text_collect():
    events = [
        ('text', 'Hi', None),
        ('tool_call', None, {'id': 'abc', 'name': 'foo', 'arguments': {}}),
        ('usage', None, {'input_tokens': 1, 'output_tokens': 1}),
    ]
    p = FakeProvider(events=events)
    r = p.chat('sys', 'usr')
    # chat() is the blocking collector — it's for batch reports, not tool use.
    # Tool-call events are silently dropped here.
    assert r['content'] == 'Hi'


def test_chat_empty_stream_returns_empty_content():
    p = FakeProvider(events=[])
    r = p.chat('sys', 'usr')
    assert r['content'] == ''
    assert r['usage']['input_tokens'] == 0


# -------------------------------------------------- TrackedLLMProvider


def test_tracked_accumulates_totals():
    events_1 = [('text', 'a', None), ('usage', None, {'input_tokens': 10, 'output_tokens': 5})]
    p = TrackedFakeProvider(events=events_1, cost_per_call=0.02)
    r1 = p.chat('sys', 'msg1')
    # chat() doesn't auto-track — subclass wires _track() in its chat override.
    # Manual call to _track() simulates what morning_engine subclasses do.
    p._track(r1['usage'])
    summary = p.get_usage_summary()
    assert summary['total_input_tokens'] == 10
    assert summary['total_output_tokens'] == 5
    assert summary['total_cost'] == 0.02


def test_tracked_is_thread_safe():
    p = TrackedFakeProvider(events=[])

    def hit():
        for _ in range(100):
            p._track({'input_tokens': 1, 'output_tokens': 1, 'cost': 0.001})

    threads = [threading.Thread(target=hit) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    summary = p.get_usage_summary()
    assert summary['total_input_tokens'] == 1000
    assert summary['total_output_tokens'] == 1000
    assert abs(summary['total_cost'] - 1.0) < 0.001  # 10 threads × 100 × 0.001


# ------------------------------------------------------- _iter_sse helper


def test_iter_sse_parses_data_frames_and_stops_on_done():
    lines = [
        'event: message',
        'data: {"foo": 1}',
        '',
        'data: {"bar": 2}',
        'data: [DONE]',
        'data: {"should": "not_appear"}',
    ]
    fake_resp = MagicMock()
    fake_resp.iter_lines.return_value = iter(lines)

    events = list(LLMProvider._iter_sse(fake_resp))
    assert events == [{'foo': 1}, {'bar': 2}]
    assert fake_resp.encoding == 'utf-8'


def test_iter_sse_skips_invalid_json():
    lines = [
        'data: {"ok": true}',
        'data: not valid json',
        'data: {"also": "ok"}',
    ]
    fake_resp = MagicMock()
    fake_resp.iter_lines.return_value = iter(lines)

    events = list(LLMProvider._iter_sse(fake_resp))
    assert len(events) == 2


# ---------------------------------- _normalize_openai_reasoning_tokens


def test_normalize_xai_reasoning_adjusts_completion_tokens():
    usage = {
        'prompt_tokens': 100,
        'completion_tokens': 50,
        'completion_tokens_details': {'reasoning_tokens': 80},
    }
    LLMProvider._normalize_openai_reasoning_tokens(usage)
    # 50 < 80 → adjusts up to 50 + 80 = 130
    assert usage['completion_tokens'] == 130


def test_normalize_xai_reasoning_leaves_usage_unchanged_when_tokens_higher():
    usage = {
        'prompt_tokens': 100,
        'completion_tokens': 200,
        'completion_tokens_details': {'reasoning_tokens': 50},
    }
    LLMProvider._normalize_openai_reasoning_tokens(usage)
    # 200 > 50 → no change
    assert usage['completion_tokens'] == 200


def test_normalize_xai_reasoning_handles_missing_details():
    usage = {'prompt_tokens': 10, 'completion_tokens': 5}
    LLMProvider._normalize_openai_reasoning_tokens(usage)
    assert usage == {'prompt_tokens': 10, 'completion_tokens': 5}


def test_normalize_xai_reasoning_handles_empty_usage():
    usage = {}
    result = LLMProvider._normalize_openai_reasoning_tokens(usage)
    assert result == {}


# ------------------------------------------- for_model factory dispatch


def test_for_model_claude_imports_anthropic_provider(monkeypatch):
    """Sanity: 'claude-*' model dispatches to AnthropicProvider.

    We stub the concrete class so the test doesn't need Flask app / API key.
    """
    import sys, types
    fake_mod = types.ModuleType('carrac.llm.anthropic')
    called = {'ok': False}
    class FakeAP:
        def __init__(self, *, model, **kwargs):
            called['ok'] = True
            called['model'] = model
    fake_mod.AnthropicProvider = FakeAP
    monkeypatch.setitem(sys.modules, 'carrac.llm.anthropic', fake_mod)

    LLMProvider.for_model('claude-opus-4-6')
    assert called['ok']
    assert called['model'] == 'claude-opus-4-6'


def test_for_model_non_claude_dispatches_to_openai_compat(monkeypatch):
    import sys, types
    fake_mod = types.ModuleType('carrac.llm.openai_compat')
    called = {'ok': False}
    class FakeOAC:
        def __init__(self, *, model, **kwargs):
            called['ok'] = True
            called['model'] = model
    fake_mod.OpenAICompatProvider = FakeOAC
    monkeypatch.setitem(sys.modules, 'carrac.llm.openai_compat', fake_mod)

    LLMProvider.for_model('grok-4-fast-reasoning')
    assert called['ok']
    assert called['model'] == 'grok-4-fast-reasoning'
