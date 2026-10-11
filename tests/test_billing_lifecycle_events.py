"""Billing lifecycle and webhook-handling tests.

Covers refund preserves paid time (verify), subscription.paused,
drawing_delete version predicate (verify), sweep_expired_subs,
audit_log, topup tier gate, subscription_id from items,
customer.created quarantine + drain, idempotency ordering and atomic AI
credits (verify existing).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_SIGNING_KEY = "".join(["pdl", "_unit_test_key_", "x" * 40])


def _sign(body: str, key: str = TEST_SIGNING_KEY, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    signed = f"{ts}:{body}"
    h1 = hmac.new(key.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_billing_idempotency.py)
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

    # Close the singleton's existing thread-local SQLite connection and
    # redirect db_path to this test's private tmp directory.  Without this,
    # xdist workers keep reopening the shared data/users.db file: state
    # (users, pending_paddle_events, …) leaks between tests that run in the
    # same worker, causing intermittent assertion failures.
    if hasattr(_udb._local, 'conn') and _udb._local.conn is not None:
        try:
            _udb._local.conn.close()
        except Exception:
            pass
        _udb._local.conn = None
    if hasattr(_udb._local, 'pg_conn'):
        _udb._local.pg_conn = None

    from pathlib import Path
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
# Full refund downgrades tier, partial preserves paid time (verify)
# ---------------------------------------------------------------------------

class TestRefundPreservesPaidTime:
    """Verify the fix: partial refund does NOT wipe subscription expiry."""

    def test_partial_refund_preserves_tier_and_expiry(self, fresh_db):
        uid = _make_user(fresh_db, "refund-partial@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_p1')
        user_before = fresh_db.get_user_by_id(uid)
        assert user_before['tier'] == 'premium'
        expiry_before = user_before['subscription_expires_at']
        assert expiry_before is not None

        # Partial refund — tier must be preserved
        fresh_db.refund_subscription(uid, full=False, refund_amount_cents=500, event_id='evt_pr1')
        user_after = fresh_db.get_user_by_id(uid)
        assert user_after['tier'] == 'premium', "Partial refund must not downgrade tier"
        assert user_after['subscription_expires_at'] == expiry_before, "Expiry must be unchanged"

    def test_full_refund_downgrades_tier(self, fresh_db):
        uid = _make_user(fresh_db, "refund-full@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_f1')
        fresh_db.refund_subscription(uid, full=True, refund_amount_cents=6900, event_id='evt_fr1')
        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'free', "Full refund must downgrade to free"


# ---------------------------------------------------------------------------
# subscription.paused handler
# ---------------------------------------------------------------------------

class TestSubscriptionPaused:
    def test_paused_preserves_tier_until_expiry(self, fresh_db):
        """subscription.paused must NOT immediately downgrade the user; the expiry
        sweep / lazy check handles the downgrade when the billing period ends."""
        from carrac.billing import routes as br

        uid = _make_user(fresh_db, "paused@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='sub_p1')
        expiry_before = fresh_db.get_user_by_id(uid)['subscription_expires_at']

        with patch('carrac.billing.routes.send_telegram_message'):
            data = {'id': 'sub_pause_1', 'customer_id': 'ctm_pause', 'custom_data': {'user_id': uid}}
            br._handle_subscription_paused(data, event_id='evt_pause_1')

        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'premium', "Paused subscription must preserve tier until expiry"
        assert user['subscription_expires_at'] == expiry_before

    def test_paused_idempotent(self, fresh_db):
        """Replaying the same paused event_id is a no-op (idempotency via marker)."""
        from carrac.billing import routes as br

        uid = _make_user(fresh_db, "paused-idem@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='sub_p2')

        with patch('carrac.billing.routes.send_telegram_message'):
            data = {'id': 'sub_pause_2', 'customer_id': 'ctm_p2', 'custom_data': {'user_id': uid}}
            br._handle_subscription_paused(data, event_id='evt_pause_2')
            br._handle_subscription_paused(data, event_id='evt_pause_2')  # replay

        conn = fresh_db._get_conn()
        markers = conn.execute(
            "SELECT COUNT(*) AS c FROM processed_payments WHERE payment_id = ?",
            ('evt_pause_2',),
        ).fetchone()
        assert markers['c'] == 1, "Idempotency: only one marker row"


# ---------------------------------------------------------------------------
# sweep_expired_subs.py
# ---------------------------------------------------------------------------

class TestSweepExpiredSubs:
    def test_sweep_downgrades_expired_cancelled_user(self, fresh_db, monkeypatch):
        """User with past expiry is downgraded; active user is skipped."""
        from scripts.sweep_expired_subs import run_sweep

        # Create expired user
        uid_expired = _make_user(fresh_db, "expired@example.com")
        fresh_db.activate_subscription(uid_expired, tier='premium', days=1, payment_id='pay_old')
        # Force expiry into the past (beyond grace)
        past_expiry = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn = fresh_db._get_conn()
        conn.execute("UPDATE users SET subscription_expires_at = ? WHERE id = ?", (past_expiry, uid_expired))
        conn.commit()

        # Create active user
        uid_active = _make_user(fresh_db, "active@example.com")
        fresh_db.activate_subscription(uid_active, tier='premium', days=30, payment_id='pay_active')

        # Create already-free user
        uid_free = _make_user(fresh_db, "free@example.com")

        count = run_sweep()

        assert count == 1
        assert fresh_db.get_user_by_id(uid_expired)['tier'] == 'free'
        assert fresh_db.get_user_by_id(uid_active)['tier'] == 'premium'
        assert fresh_db.get_user_by_id(uid_free)['tier'] == 'free'

    def test_sweep_idempotent(self, fresh_db):
        """Running sweep twice does not double-process."""
        from scripts.sweep_expired_subs import run_sweep

        uid = _make_user(fresh_db, "idem-sweep@example.com")
        fresh_db.activate_subscription(uid, tier='member', days=1, payment_id='pay_idem')
        past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn = fresh_db._get_conn()
        conn.execute("UPDATE users SET subscription_expires_at = ? WHERE id = ?", (past, uid))
        conn.commit()

        count1 = run_sweep()
        count2 = run_sweep()
        assert count1 == 1
        assert count2 == 0  # already free


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_admin_tier_override_writes_audit_row(self, fresh_db, monkeypatch):
        """Calling set_user_tier via admin route writes an audit_log row."""
        from carrac.audit import audit_log as _al

        uid = _make_user(fresh_db, "audit-subject@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_aud')

        # Call audit_log.log directly (mirrors what admin route does)
        _al.log(
            action=_al.TIER_OVERRIDE,
            actor=999,
            subject=uid,
            before={'tier': 'premium'},
            after={'tier': 'free'},
            source='admin_ui',
            note='test override',
        )

        conn = fresh_db._get_conn()
        rows = conn.execute(
            "SELECT action, actor_user_id, subject_user_id, before_json, after_json, source "
            "FROM audit_log WHERE subject_user_id = ?",
            (uid,),
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row['action'] == 'tier_override'
        assert row['actor_user_id'] == 999
        assert row['source'] == 'admin_ui'
        import json as _json
        assert _json.loads(row['before_json'])['tier'] == 'premium'
        assert _json.loads(row['after_json'])['tier'] == 'free'

    def test_audit_log_failure_does_not_raise(self, fresh_db, monkeypatch):
        """Audit log write failure must not propagate to the caller."""
        from carrac.audit import audit_log as _al
        from user_db import user_db as _udb

        # Make _get_conn raise to simulate DB failure
        original = _udb._get_conn
        def _bad_get_conn():
            raise RuntimeError("DB is down")
        monkeypatch.setattr(_udb, '_get_conn', _bad_get_conn)

        # Must not raise
        _al.log(action='tier_override', actor=1, subject=2, source='test')

        # Restore
        monkeypatch.setattr(_udb, '_get_conn', original)


# ---------------------------------------------------------------------------
# Topup tier gate
# ---------------------------------------------------------------------------

class TestTopupGate:
    def test_free_tier_topup_check_returns_false(self, fresh_db):
        """_tier_allows_topup returns False for a free-tier user."""
        from carrac.billing.routes import _tier_allows_topup

        uid = _make_user(fresh_db, "free-gate@example.com")
        # fresh user is on 'free' tier
        result = _tier_allows_topup(uid)
        assert result is False

    def test_premium_tier_topup_check_returns_true(self, fresh_db):
        """_tier_allows_topup returns True for a premium-tier user."""
        from carrac.billing.routes import _tier_allows_topup

        uid = _make_user(fresh_db, "premium-gate@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_gate')
        result = _tier_allows_topup(uid)
        assert result is True

    # GET /api/billing/topup-check is served by FastAPI.
    # Tier-gating contract is covered by tests/fastapi/test_auth.py::TestRequireTier
    # and the billing-status auth gate in tests/fastapi/test_admin_billing_json.py.


# ---------------------------------------------------------------------------
# subscription_id extraction from items fallback
# ---------------------------------------------------------------------------

class TestSubscriptionIdFromItems:
    def test_subscription_id_resolved_from_items(self, fresh_db):
        """If subscription_id is absent at top level but present in items[0],
        the mapping must still be stored."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "sub-items@example.com")

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREMIUM': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})):
            # subscription_id only in items[0], not at top level
            data = {
                'id': 'txn_items_1',
                'customer_id': 'ctm_items',
                'custom_data': {'user_id': uid},
                # No top-level subscription_id
                'items': [{
                    'price': {'id': 'pri_PREMIUM'},
                    'subscription_id': 'sub_items_1',
                }],
            }
            br._handle_transaction_completed(data, event_id='evt_items_1')

        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'premium'
        assert user.get('paddle_subscription_id') == 'sub_items_1', (
            "subscription_id from items[0] must be stored on the user"
        )


# ---------------------------------------------------------------------------
# customer.created quarantine + drain
# ---------------------------------------------------------------------------

class TestCustomerCreatedQuarantine:
    def test_unresolved_transaction_is_quarantined(self, fresh_db):
        """transaction.completed with no customer mapping quarantines the event
        instead of silently dropping it."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        # No user created with this customer_id — resolver returns None
        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREM': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})):
            data = {
                'id': 'txn_q1',
                'customer_id': 'ctm_unknown_xyz',
                # no custom_data — resolver cannot find a user
                'items': [{'price': {'id': 'pri_PREM'}}],
            }
            br._handle_transaction_completed(data, event_id='evt_q1')

        conn = fresh_db._get_conn()
        row = conn.execute(
            "SELECT event_id, event_type FROM pending_paddle_events WHERE event_id = ?",
            ('evt_q1',),
        ).fetchone()
        assert row is not None, "Unresolvable event must be quarantined"
        assert row['event_type'] == 'transaction.completed'

    def test_customer_created_drains_pending_events(self, fresh_db):
        """When customer.created arrives after a quarantined transaction.completed,
        the transaction must be replayed and the entitlement granted."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "drain@example.com")
        paddle_cid = 'ctm_drain_1'

        # Manually insert a quarantined transaction event
        txn_data = {
            'id': 'txn_drain_1',
            'customer_id': paddle_cid,
            'custom_data': {'user_id': uid},
            'items': [{'price': {'id': 'pri_PREM_DRAIN'}}],
        }
        conn = fresh_db._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO pending_paddle_events "
            "(paddle_customer_id, event_id, event_type, event_json) VALUES (?, ?, ?, ?)",
            (paddle_cid, 'evt_drain_1', 'transaction.completed', json.dumps(txn_data)),
        )
        conn.commit()

        # Now customer.created arrives — should drain and replay the txn
        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREM_DRAIN': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})):
            customer_data = {
                'id': paddle_cid,
                'custom_data': {'user_id': uid},
            }
            br._handle_customer_created(customer_data, event_id='evt_cust_1')

        # Check drain: pending event marked processed
        conn = fresh_db._get_conn()
        pending = conn.execute(
            "SELECT processed_at FROM pending_paddle_events WHERE event_id = ?",
            ('evt_drain_1',),
        ).fetchone()
        assert pending is not None
        assert pending['processed_at'] is not None, "Pending event must be marked processed after drain"

        # Tier was granted
        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'premium', "Drained transaction must have granted premium tier"


# ---------------------------------------------------------------------------
# Verify idempotency ordering (gate BEFORE mutation) — existing code
# ---------------------------------------------------------------------------

class TestIdempotencyOrdering:
    def test_duplicate_transaction_no_double_entitlement(self, fresh_db):
        """Same event_id delivered twice → tier set once, no duplicate credit."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "idem394@example.com")

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREM': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})):
            data = {
                'customer_id': 'ctm_idem394',
                'custom_data': {'user_id': uid},
                'items': [{'price': {'id': 'pri_PREM'}}],
            }
            br._handle_transaction_completed(data, event_id='evt_idem394')
            br._handle_transaction_completed(data, event_id='evt_idem394')  # replay

        conn = fresh_db._get_conn()
        markers = conn.execute(
            "SELECT COUNT(*) AS c FROM processed_payments WHERE payment_id = ?",
            ('evt_idem394',),
        ).fetchone()
        assert markers['c'] == 1


# ---------------------------------------------------------------------------
# add_ai_credits + record_ai_deposit atomic (verify existing)
# ---------------------------------------------------------------------------

class TestAtomicCredits:
    def test_credits_and_deposit_committed_together(self, fresh_db):
        """add_ai_credits + record_ai_deposit inside atomic helper → both or neither."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "atomic395@example.com")
        fresh_db.activate_subscription(uid, tier='premium', days=30, payment_id='pay_a395')

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier', staticmethod(lambda: {})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {'pri_TOPUP': 2500})):
            data = {
                'customer_id': 'ctm_a395',
                'custom_data': {'user_id': uid},
                'items': [{'price': {'id': 'pri_TOPUP'}}],
            }
            br._handle_transaction_completed(data, event_id='evt_a395')

        balance = fresh_db.get_ai_balance(uid)
        conn = fresh_db._get_conn()
        deposit_rows = conn.execute(
            "SELECT amount_cents FROM ai_deposits WHERE user_id = ? AND nowpayments_payment_id = ?",
            (uid, 'evt_a395'),
        ).fetchall()
        assert balance == 2500
        assert len(deposit_rows) == 1
        assert deposit_rows[0]['amount_cents'] == 2500


# ---------------------------------------------------------------------------
# Webhook inbox
# ---------------------------------------------------------------------------

# Webhook-inbox HTTP coverage lives in
# tests/fastapi/test_billing_paddle.py (the Paddle webhook is on FastAPI). The _inbox_record / _inbox_mark_processed unit
# behaviour is also exercised by TestPaddleHappyPath in
# tests/test_billing_idempotency.py via direct _handle_* calls.
