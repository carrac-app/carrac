"""Internal-key flag: settle(is_internal=True) must
skip the ai_usage_log row the weekly usage review sums, while credit deduction
and the global daily-spend counter stay unconditional (safety-cap invariant).

Unit tests with mocked DB + Redis. No network, no real LLM call.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DB_TYPE", "sqlite")
os.environ.setdefault("TESTING", "true")


@pytest.fixture()
def app_ctx(tmp_path, monkeypatch):
    from flask import Flask

    app = Flask(__name__)
    monkeypatch.setenv('AI_LOG_DIR', str(tmp_path / 'ai-logs'))
    with app.app_context():
        yield app


class TestSettleInternalKey:
    def test_log_ai_usage_skipped_when_internal(self, app_ctx):
        from carrac.ai import billing

        log_calls = []
        fake_user_db = MagicMock()
        fake_user_db.log_ai_usage.side_effect = lambda **kw: log_calls.append(kw)
        fake_user_db.deduct_ai_credits.return_value = True
        fake_user_db.refund_ai_credits.return_value = True

        with patch.dict('sys.modules', {'user_db': MagicMock(user_db=fake_user_db)}):
            with patch.object(billing, '_increment_global_spend') as mock_incr, \
                 patch.object(billing, '_persist_ai_log'):
                charged = billing.settle(
                    user_id=42,
                    estimate_cents=25,
                    input_tokens=200,
                    output_tokens=500,
                    feature='journal_review',
                    is_internal=True,
                )

        assert log_calls == []  # the actual exclusion
        assert mock_incr.called  # global safety cap still sees the real spend
        assert charged > 0  # credit accounting untouched

    def test_log_ai_usage_recorded_when_not_internal(self, app_ctx):
        """Control: default is_internal=False keeps logging."""
        from carrac.ai import billing

        log_calls = []
        fake_user_db = MagicMock()
        fake_user_db.log_ai_usage.side_effect = lambda **kw: log_calls.append(kw)
        fake_user_db.deduct_ai_credits.return_value = True

        with patch.dict('sys.modules', {'user_db': MagicMock(user_db=fake_user_db)}):
            with patch.object(billing, '_increment_global_spend'), \
                 patch.object(billing, '_persist_ai_log'):
                billing.settle(
                    user_id=42, estimate_cents=25, input_tokens=200,
                    output_tokens=500, feature='journal_review',
                )

        assert len(log_calls) == 1


class TestReviewTradeInternalFlagPlumbing:
    def test_review_trade_passes_is_internal_to_settle(self, app_ctx, monkeypatch):
        """CARRAC internal-key flag: review_trade(is_internal=True) must
        propagate to settle() so trade_get_ai_review (MCP) can suppress
        spend-attribution end to end."""
        from carrac.journal import ai_review

        captured = {}

        def _fake_settle(*args, **kwargs):
            captured.update(kwargs)
            return 5

        fake_db = MagicMock()
        fake_db.get_journal_entry.return_value = {
            'user_id': 1, 'closed_at': '2026-07-28T00:00:00Z', 'asset': 'BTC',
        }
        fake_db.get_trade_history.return_value = []
        fake_db.get_score_snapshots.return_value = []

        with patch.object(ai_review, 'settle', side_effect=_fake_settle), \
             patch.object(ai_review, 'check_and_reserve', return_value=True), \
             patch.object(ai_review, '_complete_chat', return_value=('{}', {})):
            ai_review.review_trade(fake_db, 1, 1, is_internal=True)

        assert captured.get('is_internal') is True
