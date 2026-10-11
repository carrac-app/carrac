"""Tests for payment-readiness fixes.

Covers:
- Fix 4: subscription.canceled sets fallback expiry when subscription_expires_at is NULL
- Fix 5: activate_subscription produces timezone-aware UTC expiry
- Fix 6: _derive_subscription_days parses Paddle billing-period fields; falls back to 30
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_billing_lifecycle_events.py)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DATA_DIR", str(db_dir))

    from carrac import db_pool as _db_pool
    _db_pool._db_type = None

    from carrac.db_pool import _sqlite_local
    if hasattr(_sqlite_local, 'conn') and _sqlite_local.conn is not None:
        try:
            _sqlite_local.conn.close()
        except Exception:
            pass
        _sqlite_local.conn = None

    from user_db import user_db as _udb

    if hasattr(_udb._local, 'conn') and _udb._local.conn is not None:
        try:
            _udb._local.conn.close()
        except Exception:
            pass
        _udb._local.conn = None
    if hasattr(_udb._local, 'pg_conn'):
        _udb._local.pg_conn = None

    _udb.db_path = Path(db_dir) / "users.db"
    _udb._init_database()

    return _udb


def _make_user(db, email: str = "test@example.com") -> int:
    import uuid
    unique_email = f"{uuid.uuid4().hex[:8]}-{email}"
    uid = db.create_user(email=unique_email, password='x' * 12, display_name='test')
    if uid is None:
        existing = db.get_user_by_email(unique_email)
        if existing:
            uid = existing.get('id')
    assert uid is not None
    return uid


# ---------------------------------------------------------------------------
# Fix 4: subscription.canceled sets fallback expiry when NULL
# ---------------------------------------------------------------------------

class TestCanceledNullExpiry:
    def test_null_expiry_gets_fallback_after_cancel(self, fresh_db, caplog):
        """A user whose subscription_expires_at is NULL after admin override
        must get a sweep-visible deadline when subscription.canceled fires."""
        from carrac.billing import routes as br

        uid = _make_user(fresh_db, "cancel-null@example.com")
        # Manually set user to premium with NULL expiry (simulating admin override)
        conn = fresh_db._get_conn()
        conn.execute(
            "UPDATE users SET tier = 'premium', subscription_expires_at = NULL WHERE id = ?",
            (uid,),
        )
        conn.commit()

        user_before = fresh_db.get_user_by_id(uid)
        assert user_before['subscription_expires_at'] is None

        with patch('carrac.billing.routes.send_telegram_message'):
            data = {
                'id': 'sub_cancel_null_1',
                'customer_id': 'ctm_cancel_null',
                'custom_data': {'user_id': uid},
            }
            br._handle_subscription_canceled(data, event_id='evt_cancel_null_1')

        user_after = fresh_db.get_user_by_id(uid)
        assert user_after['subscription_expires_at'] is not None, (
            "subscription_expires_at must be set after canceled event with NULL expiry"
        )

        # The fallback expiry should be roughly SUBSCRIPTION_GRACE_DAYS from now
        from user_db import SUBSCRIPTION_GRACE_DAYS
        expiry_dt = datetime.fromisoformat(user_after['subscription_expires_at'])
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        expected = now + timedelta(days=SUBSCRIPTION_GRACE_DAYS)
        # Allow ±60s clock drift
        assert abs((expiry_dt - expected).total_seconds()) < 60, (
            f"Fallback expiry {expiry_dt} should be ~{SUBSCRIPTION_GRACE_DAYS}d from now"
        )

    def test_existing_expiry_not_overwritten_on_cancel(self, fresh_db):
        """A user who already has subscription_expires_at set must NOT have it
        modified by the canceled handler."""
        from carrac.billing import routes as br

        uid = _make_user(fresh_db, "cancel-expiry@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_c1')
        user_before = fresh_db.get_user_by_id(uid)
        expiry_before = user_before['subscription_expires_at']
        assert expiry_before is not None

        with patch('carrac.billing.routes.send_telegram_message'):
            data = {
                'id': 'sub_cancel_2',
                'customer_id': 'ctm_cancel_2',
                'custom_data': {'user_id': uid},
            }
            br._handle_subscription_canceled(data, event_id='evt_cancel_2')

        user_after = fresh_db.get_user_by_id(uid)
        assert user_after['subscription_expires_at'] == expiry_before, (
            "Existing expiry must not be overwritten by the canceled handler"
        )

    def test_canceled_idempotent(self, fresh_db):
        """Replaying the same event_id is a no-op."""
        from carrac.billing import routes as br

        uid = _make_user(fresh_db, "cancel-idem@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_idem_c')

        with patch('carrac.billing.routes.send_telegram_message'):
            data = {
                'id': 'sub_idem_cancel',
                'customer_id': 'ctm_idem_c',
                'custom_data': {'user_id': uid},
            }
            br._handle_subscription_canceled(data, event_id='evt_idem_cancel')
            br._handle_subscription_canceled(data, event_id='evt_idem_cancel')  # replay

        conn = fresh_db._get_conn()
        markers = conn.execute(
            "SELECT COUNT(*) AS c FROM processed_payments WHERE payment_id = ?",
            ('evt_idem_cancel',),
        ).fetchone()
        assert markers['c'] == 1, "Idempotency: only one marker row"


# ---------------------------------------------------------------------------
# Fix 5: activate_subscription produces timezone-aware UTC expiry
# ---------------------------------------------------------------------------

class TestActivateSubscriptionTimezone:
    def test_new_subscription_expiry_is_timezone_aware(self, fresh_db):
        """activate_subscription must write a timezone-aware ISO expiry string."""
        uid = _make_user(fresh_db, "tz-new@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_tz1')

        user = fresh_db.get_user_by_id(uid)
        expiry_str = user['subscription_expires_at']
        assert expiry_str is not None

        expiry_dt = datetime.fromisoformat(expiry_str)
        assert expiry_dt.tzinfo is not None, (
            f"activate_subscription should produce a timezone-aware expiry; got {expiry_str!r}"
        )

    def test_extend_from_future_expiry_is_timezone_aware(self, fresh_db):
        """Extending a future subscription also yields a timezone-aware expiry."""
        uid = _make_user(fresh_db, "tz-extend@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_tz2')
        # Extend
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_tz3')

        user = fresh_db.get_user_by_id(uid)
        expiry_str = user['subscription_expires_at']
        expiry_dt = datetime.fromisoformat(expiry_str)
        assert expiry_dt.tzinfo is not None, (
            f"Extended expiry should be timezone-aware; got {expiry_str!r}"
        )

    def test_naive_existing_expiry_compared_correctly(self, fresh_db):
        """If the DB somehow has a naive expiry string, activate_subscription
        normalizes it for comparison and still returns a timezone-aware new expiry."""
        uid = _make_user(fresh_db, "tz-naive@example.com")
        # Write a naive expiry directly into the DB (simulating old data)
        future_naive = (datetime.now() + timedelta(days=20)).isoformat()
        conn = fresh_db._get_conn()
        conn.execute(
            "UPDATE users SET tier = 'premium', subscription_expires_at = ? WHERE id = ?",
            (future_naive, uid),
        )
        conn.commit()

        # activate_subscription should extend from the naive expiry, not from now
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_tz_naive')

        user = fresh_db.get_user_by_id(uid)
        expiry_str = user['subscription_expires_at']
        expiry_dt = datetime.fromisoformat(expiry_str)
        assert expiry_dt.tzinfo is not None, (
            f"Result expiry must be timezone-aware even when source was naive; got {expiry_str!r}"
        )
        # Should be ~50 days from now (20 remaining + 30 added)
        now = datetime.now(timezone.utc)
        assert (expiry_dt - now).days >= 45


# ---------------------------------------------------------------------------
# Fix 6: _derive_subscription_days
# ---------------------------------------------------------------------------

class TestDeriveSubscriptionDays:
    def test_annual_billing_period_returns_approx_365(self):
        """An annual billing period end date ~365 days away returns ~365."""
        from carrac.billing.routes import _derive_subscription_days

        future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        data = {
            'subscription': {
                'current_billing_period': {
                    'ends_at': future,
                }
            }
        }
        days = _derive_subscription_days(data)
        assert 360 <= days <= 370, f"Expected ~365, got {days}"

    def test_monthly_billing_period_returns_approx_30(self):
        """A monthly billing period end date ~30 days away returns ~30."""
        from carrac.billing.routes import _derive_subscription_days

        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        data = {
            'subscription': {
                'current_billing_period': {
                    'ends_at': future,
                }
            }
        }
        days = _derive_subscription_days(data)
        assert 28 <= days <= 32, f"Expected ~30, got {days}"

    def test_next_billed_at_fallback_path(self):
        """next_billed_at is tried when current_billing_period is absent."""
        from carrac.billing.routes import _derive_subscription_days

        future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        data = {'subscription': {'next_billed_at': future}}
        days = _derive_subscription_days(data)
        assert 360 <= days <= 370

    def test_past_ends_at_falls_back_to_default(self):
        """A stale/past ends_at (e.g. long-paused resume) must not grant a 1-day sub."""
        from carrac.billing.routes import _derive_subscription_days

        past = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        data = {'subscription': {'current_billing_period': {'ends_at': past}}}
        assert _derive_subscription_days(data) == 30

    def test_past_ends_at_with_valid_next_billed_at_uses_next(self):
        """A stale period end is skipped; a valid later key path still wins."""
        from carrac.billing.routes import _derive_subscription_days

        past = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        data = {
            'subscription': {
                'current_billing_period': {'ends_at': past},
                'next_billed_at': future,
            }
        }
        assert 28 <= _derive_subscription_days(data) <= 32

    def test_top_level_next_billed_at(self):
        """Top-level next_billed_at (no subscription wrapper) also works."""
        from carrac.billing.routes import _derive_subscription_days

        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        data = {'next_billed_at': future}
        days = _derive_subscription_days(data)
        assert 28 <= days <= 32

    def test_missing_billing_period_falls_back_to_default(self):
        """No billing period fields → default 30."""
        from carrac.billing.routes import _derive_subscription_days

        data = {'id': 'txn_no_period', 'customer_id': 'ctm_x'}
        days = _derive_subscription_days(data)
        assert days == 30

    def test_custom_default_respected(self):
        """Custom default is returned when no billing period is present."""
        from carrac.billing.routes import _derive_subscription_days

        days = _derive_subscription_days({}, default=7)
        assert days == 7

    def test_z_suffix_iso_string_parsed_correctly(self):
        """Paddle uses 'Z' suffix; _derive_subscription_days must handle it."""
        from carrac.billing.routes import _derive_subscription_days

        future = (datetime.now(timezone.utc) + timedelta(days=365)).strftime('%Y-%m-%dT%H:%M:%SZ')
        data = {'subscription': {'current_billing_period': {'ends_at': future}}}
        days = _derive_subscription_days(data)
        assert 360 <= days <= 370

    def test_transaction_completed_uses_billing_period_days(self, fresh_db):
        """_handle_transaction_completed passes billing-period days to activate_subscription."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "billing-period@example.com")
        future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_ANNUAL': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})):
            data = {
                'id': 'txn_annual_1',
                'customer_id': 'ctm_annual',
                'custom_data': {'user_id': uid},
                'items': [{'price': {'id': 'pri_ANNUAL'}}],
                'subscription': {
                    'current_billing_period': {
                        'ends_at': future,
                    }
                },
            }
            br._handle_transaction_completed(data, event_id='evt_annual_1')

        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'premium'
        expiry_str = user['subscription_expires_at']
        assert expiry_str is not None
        expiry_dt = datetime.fromisoformat(expiry_str)
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        # With ~365 days, expiry should be ~365 days from now (not 30)
        days_until = (expiry_dt - now).days
        assert days_until >= 350, (
            f"Annual billing period should yield ~365d expiry; got {days_until}d"
        )
