"""AI SSE mid-stream disconnect cost-leak tests.

GeneratorExit (browser tab close) must not refund the full reserved estimate,
because the provider keeps billing for the tokens already consumed. Otherwise
a tab-close loop would bypass the per-user balance AND the global daily AI cap.

`settle(disconnected=True)`:
1. Computes raw cost from the real tokens already streamed.
2. Skips response_text body persistence (the response was never delivered).
3. ALWAYS calls _increment_global_spend() so the daily cap can trip.

These are unit tests with mocked DB + Redis. No network, no real LLM call.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Force SQLite + test mode before any imports
os.environ.setdefault("DB_TYPE", "sqlite")
os.environ.setdefault("TESTING", "true")


@pytest.fixture()
def app_ctx(tmp_path, monkeypatch):
    """Minimal Flask app context for billing.settle()."""
    from flask import Flask

    app = Flask(__name__)
    app.config['AI_PROVIDER'] = 'anthropic'
    app.config['AI_MODEL'] = 'claude-opus-4-7'
    app.config['AI_CONSERVATIVE_ESTIMATE_CENTS'] = 25
    app.config['AI_INPUT_RATE_PER_M'] = 16.0
    app.config['AI_OUTPUT_RATE_PER_M'] = 80.0
    app.config['CREDIT_MARKUP'] = 4.0
    # billing._daily_cap_cents reads this from os.getenv (not app.config),
    # so set the env var — otherwise the cap stays at the 30 USD default
    # and the over-cap assertions in TestDisconnectGlobalCap silently fail.
    monkeypatch.setenv('AI_GLOBAL_DAILY_USD_CAP', '0.50')  # 50c — easy to trip
    monkeypatch.setenv('AI_LOG_DIR', str(tmp_path / 'ai-logs'))

    with app.app_context():
        yield app


# ---------------------------------------------------------------------------
# settle(disconnected=True) — unit
# ---------------------------------------------------------------------------

class TestSettleDisconnect:
    def test_logs_usage_with_real_tokens_and_skips_response_body(self, app_ctx, monkeypatch):
        """500 output tokens consumed → ai_usage_log row exists with
        output_tokens ≈ 500, cost_cents > 0, no response_text body persisted."""
        from carrac.ai import billing

        log_calls = []
        refund_calls = []
        deduct_calls = []

        fake_user_db = MagicMock()
        fake_user_db.log_ai_usage.side_effect = lambda **kw: log_calls.append(kw)
        fake_user_db.refund_ai_credits.side_effect = lambda uid, amt: refund_calls.append((uid, amt))
        fake_user_db.deduct_ai_credits.side_effect = lambda uid, amt: deduct_calls.append((uid, amt)) or True

        with patch.dict('sys.modules', {'user_db': MagicMock(user_db=fake_user_db)}):
            with patch.object(billing, '_increment_global_spend') as mock_incr, \
                 patch.object(billing, '_persist_ai_log') as mock_persist:
                charged = billing.settle(
                    user_id=42,
                    estimate_cents=25,
                    input_tokens=200,
                    output_tokens=500,
                    feature='chat',
                    asset='BTC',
                    model='claude-opus-4-7',
                    prompt_text='hello world',
                    response_text='partial output that was never delivered',
                    conversation_id='conv-1',
                    disconnected=True,
                )

        # log_ai_usage was called with output_tokens == 500 and a feature suffix
        assert len(log_calls) == 1
        call = log_calls[0]
        assert call['output_tokens'] == 500
        assert call['input_tokens'] == 200
        assert call['cost_cents'] > 0
        # disconnect-tagged feature so dashboards can split
        assert ':disconnect' in call['feature']
        # response_sha256 NOT set on disconnect (body never delivered)
        assert call['response_sha256'] is None

        # _persist_ai_log NOT called on disconnect (body never delivered)
        mock_persist.assert_not_called()

        # global spend bumped — even on disconnect
        mock_incr.assert_called_once()
        bumped = mock_incr.call_args[0][0]
        assert bumped > 0

        # charged returned > 0
        assert charged > 0

    def test_increment_global_spend_called_on_disconnect(self, app_ctx, monkeypatch):
        """Independently verify the global-cap counter bumps even on disconnect."""
        from carrac.ai import billing

        fake_user_db = MagicMock()
        fake_user_db.deduct_ai_credits.return_value = True

        with patch.dict('sys.modules', {'user_db': MagicMock(user_db=fake_user_db)}):
            with patch.object(billing, '_increment_global_spend') as mock_incr, \
                 patch.object(billing, '_persist_ai_log'):
                billing.settle(
                    user_id=99,
                    estimate_cents=25,
                    input_tokens=100,
                    output_tokens=300,
                    feature='chat',
                    disconnected=True,
                )

        assert mock_incr.called

    def test_default_disconnected_false_persists_body(self, app_ctx):
        """Sanity: when disconnected=False (the success path), the body IS
        persisted via _persist_ai_log."""
        from carrac.ai import billing

        fake_user_db = MagicMock()
        fake_user_db.deduct_ai_credits.return_value = True

        with patch.dict('sys.modules', {'user_db': MagicMock(user_db=fake_user_db)}):
            with patch.object(billing, '_increment_global_spend'), \
                 patch.object(billing, '_persist_ai_log') as mock_persist:
                billing.settle(
                    user_id=7,
                    estimate_cents=25,
                    input_tokens=100,
                    output_tokens=200,
                    feature='chat',
                    prompt_text='p',
                    response_text='r',
                    # disconnected omitted — defaults False
                )

        mock_persist.assert_called_once()


# ---------------------------------------------------------------------------
# Daily-cap trip simulation — disconnect loop bounded by AI_GLOBAL_DAILY_USD_CAP
# ---------------------------------------------------------------------------

class TestDisconnectGlobalCap:
    def test_repeated_disconnects_increment_global_spend(self, app_ctx):
        """Simulate 100 disconnects each ~500 output tokens. Verify the
        running sum of _increment_global_spend calls equals what the
        daily-cap check would compare against."""
        from carrac.ai import billing

        fake_user_db = MagicMock()
        fake_user_db.deduct_ai_credits.return_value = True

        bumped = []

        def _capture(cents):
            bumped.append(cents)

        with patch.dict('sys.modules', {'user_db': MagicMock(user_db=fake_user_db)}):
            with patch.object(billing, '_increment_global_spend', side_effect=_capture), \
                 patch.object(billing, '_persist_ai_log'):
                for _ in range(100):
                    billing.settle(
                        user_id=1,
                        estimate_cents=25,
                        input_tokens=100,
                        output_tokens=500,
                        feature='chat',
                        disconnected=True,
                    )

        # Each disconnect bumps once with the same raw cost
        assert len(bumped) == 100
        per_call = bumped[0]
        assert per_call > 0
        assert all(b == per_call for b in bumped)

        total = sum(bumped)
        # 100 disconnects @ 500 output + 100 input tokens @ Anthropic Opus pricing
        # is well above the 50-cent test cap — confirms the cap WOULD trip.
        cap_cents = billing._daily_cap_cents()
        assert total >= cap_cents, (
            f"Expected disconnect-loop total ({total}c) to exceed daily cap "
            f"({cap_cents}c) so check_global_budget would 503. Got {total} vs {cap_cents}."
        )

    def test_check_global_budget_uses_redis_counter(self, app_ctx):
        """Wire a fake Redis client and verify check_global_budget() reads
        the same counter that _increment_global_spend writes."""
        from carrac.ai import billing

        # Fake Redis: in-memory dict
        store: dict[str, str] = {}

        class _FakePipeline:
            def __init__(self, s): self.s = s; self._ops = []
            def incrby(self, k, v):
                self._ops.append(('incrby', k, v))
                return self
            def expire(self, k, ttl):
                self._ops.append(('expire', k, ttl))
                return self
            def execute(self):
                for op in self._ops:
                    if op[0] == 'incrby':
                        self.s[op[1]] = str(int(self.s.get(op[1], '0')) + op[2])

        class _FakeRedis:
            def __init__(self, s): self.s = s
            def ping(self): return True
            def get(self, k): return self.s.get(k)
            def pipeline(self): return _FakePipeline(self.s)

        fake = _FakeRedis(store)
        with patch.object(billing, '_redis_client', return_value=fake):
            # Bump 60 cents — over the 50-cent test cap.
            billing._increment_global_spend(60)
            ok, info = billing.check_global_budget()

        assert ok is False
        assert info['spent_cents'] >= 60
        assert info['cap_cents'] == 50  # configured 0.50 USD


# ---------------------------------------------------------------------------
# settle() signature — disconnected kwarg is accepted as keyword-only-friendly
# ---------------------------------------------------------------------------

class TestSettleSignature:
    def test_disconnected_kwarg_default_false(self):
        """settle() must accept `disconnected` and default it to False."""
        import inspect
        from carrac.ai.billing import settle
        sig = inspect.signature(settle)
        assert 'disconnected' in sig.parameters
        assert sig.parameters['disconnected'].default is False
