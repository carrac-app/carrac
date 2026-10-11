"""Subscription grace period and partial-refund tests.

Covers:
- check_subscription_expiry returns active (not expired) when expiry is 1 day ago (within grace)
- check_subscription_expiry sets subscription_in_grace=True when expiry is 2 days ago (within grace)
- check_subscription_expiry flips tier to free when expiry is 4 days ago (beyond grace)
- _handle_subscription_past_due extends subscription_expires_at by 7 days
- _handle_transaction_refunded with full refund → tier='free'
- _handle_transaction_refunded with partial refund (refunded < fee) → tier unchanged,
  ai_deposits row written with source='paddle_partial_refund'
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_billing_idempotency.py)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch, rebind_db):
    """Fresh isolated SQLite user_db per test.

    Delegates to the shared conftest helper (via the `rebind_db` fixture),
    which rebinds the user_db singleton's db_path AND drops its cached
    thread-local connection so each test gets a genuinely empty DB. (The
    previous version reset only the pg connection, never the sqlite one or
    db_path, so it actually shared the real ./data/users.db and had to scrub
    paddle columns by hand to stay deterministic.)
    """
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DATA_DIR", str(db_dir))
    return rebind_db(db_dir)


def _make_user(db, email: str = "test@example.com") -> int:
    unique_email = f"{uuid.uuid4().hex[:8]}-{email}"
    uid = db.create_user(email=unique_email, password='x' * 12, display_name='tester')
    if uid is None:
        existing = db.get_user_by_email(unique_email)
        if existing:
            uid = existing.get('id')
    assert uid is not None, f"could not create test user {unique_email}"
    return uid


def _set_expiry(db, user_id: int, dt: datetime):
    """Directly write subscription_expires_at for a test user."""
    conn = db._get_conn()
    conn.execute(
        "UPDATE users SET subscription_expires_at = ? WHERE id = ?",
        (dt.isoformat(), user_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Grace period tests
# ---------------------------------------------------------------------------

class TestSubscriptionGracePeriod:
    def test_within_grace_1_day_past_still_active(self, fresh_db):
        """Expiry 1 day ago — within 3-day grace — should NOT downgrade."""
        uid = _make_user(fresh_db, "grace1@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        expiry = datetime.now(timezone.utc) - timedelta(days=1)
        _set_expiry(fresh_db, uid, expiry)

        result = fresh_db.check_subscription_expiry(uid)

        assert result['expired'] is False, "should not be expired within grace"
        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'premium', "tier must not be downgraded within grace"

    def test_within_grace_2_days_past_sets_in_grace_flag(self, fresh_db):
        """Expiry 2 days ago — within grace — subscription_in_grace must be True."""
        uid = _make_user(fresh_db, "grace2@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        expiry = datetime.now(timezone.utc) - timedelta(days=2)
        _set_expiry(fresh_db, uid, expiry)

        result = fresh_db.check_subscription_expiry(uid)

        assert result['subscription_in_grace'] is True, "grace flag should be set"
        assert result['expired'] is False
        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'premium', "tier must not be downgraded within grace"

    def test_beyond_grace_4_days_past_downgrades(self, fresh_db):
        """Expiry 4 days ago — beyond 3-day grace — must flip tier to free."""
        uid = _make_user(fresh_db, "grace4@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        expiry = datetime.now(timezone.utc) - timedelta(days=4)
        _set_expiry(fresh_db, uid, expiry)

        result = fresh_db.check_subscription_expiry(uid)

        assert result['expired'] is True, "should be expired beyond grace"
        assert result['tier'] == 'free', "returned tier should be free"
        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'free', "DB tier must be downgraded beyond grace"
        assert user['subscription_expires_at'] is None, "expiry must be cleared"

    def test_still_valid_subscription_not_in_grace(self, fresh_db):
        """Expiry 5 days in the future — active subscription, grace flag False."""
        uid = _make_user(fresh_db, "valid@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        expiry = datetime.now(timezone.utc) + timedelta(days=5)
        _set_expiry(fresh_db, uid, expiry)

        result = fresh_db.check_subscription_expiry(uid)

        assert result['expired'] is False
        assert result.get('subscription_in_grace') is False
        assert result['days_remaining'] >= 4


# ---------------------------------------------------------------------------
# past_due extends subscription_expires_at by 7 days
# ---------------------------------------------------------------------------

class TestSubscriptionPastDue:
    def test_past_due_extends_expiry_by_7_days(self, fresh_db):
        """subscription.past_due must extend subscription_expires_at by 7 days."""
        from carrac.billing import routes as br

        uid = _make_user(fresh_db, "pastdue@example.com")
        fresh_db.set_user_tier(uid, 'premium')

        # Set expiry to 1 day from now (almost due)
        original_expiry = datetime.now(timezone.utc) + timedelta(days=1)
        _set_expiry(fresh_db, uid, original_expiry)

        # Wire paddle_customer_id so _resolve_user_from_event finds the user
        fresh_db.set_paddle_customer_id(uid, 'ctm_pastdue')

        data = {
            'id': 'sub_pastdue_1',
            'customer_id': 'ctm_pastdue',
            'custom_data': {'user_id': uid},
        }

        with patch('carrac.billing.routes.send_telegram_message'):
            br._handle_subscription_past_due(data, event_id='evt_pastdue_1')

        user = fresh_db.get_user_by_id(uid)
        assert user['subscription_expires_at'] is not None
        new_expiry = datetime.fromisoformat(user['subscription_expires_at'])
        if new_expiry.tzinfo is None:
            new_expiry = new_expiry.replace(tzinfo=timezone.utc)

        # New expiry should be approximately 8 days from now (1 existing + 7 added)
        expected_min = datetime.now(timezone.utc) + timedelta(days=7)
        assert new_expiry >= expected_min, (
            f"Expected expiry >= {expected_min}, got {new_expiry}"
        )

    def test_past_due_idempotent_on_replay(self, fresh_db):
        """Same past_due event delivered twice — second call is a no-op (marker)."""
        from carrac.billing import routes as br

        uid = _make_user(fresh_db, "pastdue_replay@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        fresh_db.set_paddle_customer_id(uid, 'ctm_pdreplay')

        original_expiry = datetime.now(timezone.utc) + timedelta(days=2)
        _set_expiry(fresh_db, uid, original_expiry)

        data = {
            'id': 'sub_pdreplay',
            'customer_id': 'ctm_pdreplay',
            'custom_data': {'user_id': uid},
        }

        with patch('carrac.billing.routes.send_telegram_message'):
            br._handle_subscription_past_due(data, event_id='evt_pdreplay')
            user_after_first = fresh_db.get_user_by_id(uid)
            expiry_after_first = user_after_first['subscription_expires_at']

            br._handle_subscription_past_due(data, event_id='evt_pdreplay')
            user_after_second = fresh_db.get_user_by_id(uid)
            expiry_after_second = user_after_second['subscription_expires_at']

        assert expiry_after_first == expiry_after_second, "expiry must not change on replay"


# ---------------------------------------------------------------------------
# Partial refund preserves paid period
# ---------------------------------------------------------------------------

class TestRefundBehaviour:
    def test_full_refund_downgrades_tier(self, fresh_db):
        """transaction.refunded with refunded_amount == grand_total → tier='free'."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "fullrefund@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        fresh_db.set_paddle_customer_id(uid, 'ctm_fullrefund')
        expiry = datetime.now(timezone.utc) + timedelta(days=20)
        _set_expiry(fresh_db, uid, expiry)

        data = {
            'customer_id': 'ctm_fullrefund',
            'custom_data': {'user_id': uid},
            'items': [{'price': {'id': 'pri_PREMIUM'}}],
            'details': {
                'totals': {
                    'grand_total': 2000,
                    'refund': 2000,
                }
            },
        }

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREMIUM': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})), \
             patch('carrac.billing.routes.send_telegram_message'):
            br._handle_transaction_refunded(data, event_id='evt_fullrefund_1')

        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'free', "full refund must downgrade to free"

    def test_partial_refund_preserves_tier(self, fresh_db):
        """transaction.refunded with refunded_amount < grand_total → tier unchanged."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "partrefund@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        fresh_db.set_paddle_customer_id(uid, 'ctm_partrefund')
        expiry = datetime.now(timezone.utc) + timedelta(days=25)
        _set_expiry(fresh_db, uid, expiry)

        data = {
            'customer_id': 'ctm_partrefund',
            'custom_data': {'user_id': uid},
            'items': [{'price': {'id': 'pri_PREMIUM'}}],
            'details': {
                'totals': {
                    'grand_total': 2000,
                    'refund': 500,   # $5 goodwill out of $20
                }
            },
        }

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREMIUM': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})), \
             patch('carrac.billing.routes.send_telegram_message'):
            br._handle_transaction_refunded(data, event_id='evt_partrefund_1')

        # Tier must be unchanged
        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'premium', "partial refund must not downgrade tier"

        # subscription_expires_at must still be set
        assert user['subscription_expires_at'] is not None, \
            "partial refund must not clear subscription_expires_at"

    def test_partial_refund_writes_audit_row(self, fresh_db):
        """Partial refund must write a negative ai_deposits row tagged paddle_partial_refund."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "partaudit@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        fresh_db.set_paddle_customer_id(uid, 'ctm_partaudit')
        expiry = datetime.now(timezone.utc) + timedelta(days=25)
        _set_expiry(fresh_db, uid, expiry)

        data = {
            'customer_id': 'ctm_partaudit',
            'custom_data': {'user_id': uid},
            'items': [{'price': {'id': 'pri_PREMIUM'}}],
            'details': {
                'totals': {
                    'grand_total': 2000,
                    'refund': 500,
                }
            },
        }

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREMIUM': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})), \
             patch('carrac.billing.routes.send_telegram_message'):
            br._handle_transaction_refunded(data, event_id='evt_partaudit_1')

        # Audit row: negative amount, source = 'paddle_partial_refund'
        conn = fresh_db._get_conn()
        rows = conn.execute(
            "SELECT amount_cents, crypto_amount FROM ai_deposits "
            "WHERE user_id = ? AND nowpayments_payment_id = ?",
            (uid, 'evt_partaudit_1'),
        ).fetchall()

        assert len(rows) == 1, f"expected 1 audit deposit row, got {len(rows)}"
        assert rows[0]['amount_cents'] == -500, \
            f"expected -500 cents, got {rows[0]['amount_cents']}"
        assert rows[0]['crypto_amount'] == 'paddle_partial_refund', \
            f"expected source paddle_partial_refund, got {rows[0]['crypto_amount']}"

    def test_full_refund_via_payments_array_fallback(self, fresh_db):
        """If details.totals is absent, fall back to data.payments[] to detect full refund."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "payfallback@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        fresh_db.set_paddle_customer_id(uid, 'ctm_payfallback')
        expiry = datetime.now(timezone.utc) + timedelta(days=20)
        _set_expiry(fresh_db, uid, expiry)

        data = {
            'customer_id': 'ctm_payfallback',
            'custom_data': {'user_id': uid},
            'items': [{'price': {'id': 'pri_PREMIUM'}}],
            # No details.totals — use payments array fallback
            'payments': [{'amount': 2000, 'refunded_amount': 2000}],
        }

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREMIUM': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})), \
             patch('carrac.billing.routes.send_telegram_message'):
            br._handle_transaction_refunded(data, event_id='evt_payfallback_1')

        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'free', "payments-array full refund must downgrade to free"

    def test_no_amount_data_treated_as_full_refund(self, fresh_db):
        """When no amount data is present at all, default to full refund (safe)."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "noamount@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        fresh_db.set_paddle_customer_id(uid, 'ctm_noamount')
        expiry = datetime.now(timezone.utc) + timedelta(days=20)
        _set_expiry(fresh_db, uid, expiry)

        data = {
            'customer_id': 'ctm_noamount',
            'custom_data': {'user_id': uid},
            'items': [{'price': {'id': 'pri_PREMIUM'}}],
            # No details, no payments → can't determine partial vs full
        }

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREMIUM': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})), \
             patch('carrac.billing.routes.send_telegram_message'):
            br._handle_transaction_refunded(data, event_id='evt_noamount_1')

        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'free', "ambiguous refund must default to full (safe)"
