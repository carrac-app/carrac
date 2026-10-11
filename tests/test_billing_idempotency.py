"""Paddle webhook idempotency ordering tests.

Covers the atomic marker-LAST pattern in `process_paddle_event_atomic`:
- Happy path: marker + entitlement writes commit together.
- Replay: same event_id arriving twice → first runs, second is no-op,
  no double-credit (ledger SUM(deposits) invariant holds).
- Concurrent replay: two threads racing the same event_id → exactly one
  succeeds, the loser's work is rolled back via PK violation.
- Mid-work failure: monkey-patched `activate_subscription` raises →
  marker NOT set, tier NOT flipped, retry replays cleanly.
- Empty event_id at the route boundary → HTTP 400.

These are unit tests using SQLite + Flask test client, no Paddle network.
"""
from __future__ import annotations

import hashlib
import hmac
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the signing key + helper from the sibling Paddle test module.
TEST_SIGNING_KEY = "".join(["pdl", "_unit_test_key_", "x" * 40])


def _sign(body: str, key: str = TEST_SIGNING_KEY, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    signed = f"{ts}:{body}"
    h1 = hmac.new(key.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch, rebind_db):
    """Create a fresh user_db with a unique SQLite file per test.

    Delegates to the shared conftest helper (`rebind_db`), which rebinds the
    user_db singleton's db_path AND drops its cached thread-local connection,
    giving each test a genuinely empty DB. The previous local version reset the
    db_pool sqlite handle + the pg connection but never the singleton's own
    sqlite connection or db_path, so all billing tests actually shared one
    users.db and had to scrub paddle_customer_id / processed_payments by hand to
    stay deterministic. A truly isolated DB makes that scrub unnecessary.
    """
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DATA_DIR", str(db_dir))
    return rebind_db(db_dir)


def _make_user(db, email: str = "happy@example.com") -> int:
    """Create a user and return its ID.

    Suffixes a uuid to the email so that prior tests' state in a polluted
    singleton DB (test_billing_paddle.py shares the singleton without a
    fresh_db fixture) cannot collide. The post-create lookup is a
    belt-and-braces fallback for the rare race where create_user returns
    None despite a fresh row.
    """
    import uuid
    unique_email = f"{uuid.uuid4().hex[:8]}-{email}"
    uid = db.create_user(email=unique_email, password='x' * 12, display_name='happy')
    if uid is None:
        existing = db.get_user_by_email(unique_email)
        if existing:
            uid = existing.get('id')
    assert uid is not None, f"could not create or look up test user {unique_email}"
    return uid


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestPaddleHappyPath:
    def test_subscription_activation_writes_marker_and_flips_tier(self, fresh_db):
        """transaction.completed for a subscription price → marker row exists,
        tier is 'premium', subscription_expires_at populated, all in one commit."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "sub@example.com")

        # Inject the subscription price ID into the lookup map.
        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREMIUM_30D': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})):
            data = {
                'id': 'txn_happy_1',
                'customer_id': 'ctm_happy',
                'subscription_id': 'sub_happy_1',
                'custom_data': {'user_id': uid},
                'items': [{'price': {'id': 'pri_PREMIUM_30D'}}],
            }
            br._handle_transaction_completed(data, event_id='evt_happy_1')

        # Marker row written
        conn = fresh_db._get_conn()
        marker = conn.execute(
            "SELECT user_id FROM processed_payments WHERE payment_id = ?",
            ('evt_happy_1',),
        ).fetchone()
        assert marker is not None
        assert marker['user_id'] == uid

        # Tier flipped to premium
        user = fresh_db.get_user_by_id(uid)
        assert user['tier'] == 'premium'
        assert user['subscription_expires_at'] is not None

    def test_topup_credits_added_and_deposit_recorded(self, fresh_db, monkeypatch):
        """transaction.completed for a top-up price → balance increases,
        ai_deposits row written, all atomic with the marker."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "topup@example.com")

        # Premium tier so top-ups are allowed.
        fresh_db.set_user_tier(uid, 'premium')
        # set_user_tier nulls the expiry; restore a future expiry so the
        # tier_allows_topup lookup sees a real subscription.
        from datetime import datetime, timedelta
        conn = fresh_db._get_conn()
        conn.execute(
            'UPDATE users SET subscription_expires_at = ? WHERE id = ?',
            ((datetime.now() + timedelta(days=30)).isoformat(), uid),
        )
        conn.commit()

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {'pri_TOPUP_25': 2500})):
            data = {
                'id': 'txn_topup_1',
                'customer_id': 'ctm_topup',
                'custom_data': {'user_id': uid},
                'items': [{'price': {'id': 'pri_TOPUP_25'}}],
            }
            br._handle_transaction_completed(data, event_id='evt_topup_1')

        # Balance increased by 2500 cents
        assert fresh_db.get_ai_balance(uid) == 2500

        # Deposit ledger row written
        conn = fresh_db._get_conn()
        deposits = conn.execute(
            "SELECT amount_cents FROM ai_deposits WHERE user_id = ? AND nowpayments_payment_id = ?",
            (uid, 'evt_topup_1'),
        ).fetchall()
        assert len(deposits) == 1
        assert deposits[0]['amount_cents'] == 2500


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------

class TestPaddleReplayIdempotency:
    def test_duplicate_event_id_no_double_credit(self, fresh_db):
        """Same event_id delivered twice → second call is no-op.

        Asserts ledger invariant: SUM(deposits.amount_cents) for this event
        equals the single deposit, balance is unchanged on replay.
        """
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "replay@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        from datetime import datetime, timedelta
        conn = fresh_db._get_conn()
        conn.execute(
            'UPDATE users SET subscription_expires_at = ? WHERE id = ?',
            ((datetime.now() + timedelta(days=30)).isoformat(), uid),
        )
        conn.commit()

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {'pri_TOPUP': 1000})):
            data = {
                'id': 'txn_replay_1',
                'customer_id': 'ctm_replay',
                'custom_data': {'user_id': uid},
                'items': [{'price': {'id': 'pri_TOPUP'}}],
            }
            br._handle_transaction_completed(data, event_id='evt_replay_dup')
            balance_after_first = fresh_db.get_ai_balance(uid)

            # Second delivery — same event_id
            br._handle_transaction_completed(data, event_id='evt_replay_dup')
            balance_after_second = fresh_db.get_ai_balance(uid)

        assert balance_after_first == 1000
        assert balance_after_second == 1000  # no double credit

        # SUM(deposits) invariant for this event
        conn = fresh_db._get_conn()
        total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ai_deposits "
            "WHERE user_id = ? AND nowpayments_payment_id = ?",
            (uid, 'evt_replay_dup'),
        ).fetchone()
        assert total['s'] == 1000  # one row, not two

    def test_subscription_replay_no_double_extension(self, fresh_db):
        """Duplicate transaction.completed for a subscription must not extend
        the period twice. activate_subscription extends from the existing
        expiry — a replay would silently double the granted days under the
        old marker-FIRST pattern."""
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "subreplay@example.com")

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREMIUM_30D': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})):
            data = {
                'id': 'txn_subreplay_1',
                'customer_id': 'ctm_subreplay',
                'subscription_id': 'sub_subreplay',
                'custom_data': {'user_id': uid},
                'items': [{'price': {'id': 'pri_PREMIUM_30D'}}],
            }
            br._handle_transaction_completed(data, event_id='evt_subreplay')
            user_after_first = fresh_db.get_user_by_id(uid)
            expiry_after_first = user_after_first['subscription_expires_at']

            br._handle_transaction_completed(data, event_id='evt_subreplay')
            user_after_second = fresh_db.get_user_by_id(uid)
            expiry_after_second = user_after_second['subscription_expires_at']

        assert expiry_after_first == expiry_after_second  # no extra 30 days


# ---------------------------------------------------------------------------
# Concurrent replay (PK race)
# ---------------------------------------------------------------------------

class TestPaddleConcurrentReplay:
    def test_two_threads_same_event_id_one_wins(self, fresh_db):
        """Two threads call the handler with the same event_id at the same
        time. With the atomic helper exactly one committed deposit must exist,
        and the loser's INSERTs (deposits) are rolled back via PK violation
        on the marker.
        """
        from carrac.billing import routes as br
        from carrac import config as cfg_mod

        uid = _make_user(fresh_db, "concurrent@example.com")
        fresh_db.set_user_tier(uid, 'premium')
        from datetime import datetime, timedelta
        conn = fresh_db._get_conn()
        conn.execute(
            'UPDATE users SET subscription_expires_at = ? WHERE id = ?',
            ((datetime.now() + timedelta(days=30)).isoformat(), uid),
        )
        conn.commit()

        results = []
        barrier = threading.Barrier(2)

        def _runner():
            with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                              staticmethod(lambda: {})), \
                 patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                              staticmethod(lambda: {'pri_TOPUP_RACE': 500})):
                data = {
                    'id': 'txn_race',
                    'customer_id': 'ctm_race',
                    'custom_data': {'user_id': uid},
                    'items': [{'price': {'id': 'pri_TOPUP_RACE'}}],
                }
                barrier.wait()
                try:
                    br._handle_transaction_completed(data, event_id='evt_race_1')
                    results.append('ok')
                except Exception as e:
                    results.append(f'err:{e}')

        t1 = threading.Thread(target=_runner)
        t2 = threading.Thread(target=_runner)
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Both calls return without raising — the loser silently no-ops.
        assert all(r == 'ok' for r in results), f"unexpected runner errors: {results}"

        # Exactly ONE marker, ONE deposit, balance is 500 (not 1000).
        conn = fresh_db._get_conn()
        markers = conn.execute(
            "SELECT COUNT(*) AS c FROM processed_payments WHERE payment_id = ?",
            ('evt_race_1',),
        ).fetchone()
        assert markers['c'] == 1

        deposits = conn.execute(
            "SELECT COUNT(*) AS c FROM ai_deposits "
            "WHERE user_id = ? AND nowpayments_payment_id = ?",
            (uid, 'evt_race_1'),
        ).fetchone()
        assert deposits['c'] == 1

        assert fresh_db.get_ai_balance(uid) == 500


# ---------------------------------------------------------------------------
# Mid-work failure (the core bug)
# ---------------------------------------------------------------------------

class TestPaddleMidWorkFailure:
    def test_failure_during_activate_subscription_rolls_back_marker(self, fresh_db, monkeypatch):
        """Monkey-patch user_db.activate_subscription to raise. Assert:
        - the user's tier is NOT flipped,
        - the marker is NOT in processed_payments,
        - a subsequent retry with the same event_id succeeds cleanly.
        """
        from carrac.billing import routes as br
        from carrac import config as cfg_mod
        from user_db import user_db as _udb

        uid = _make_user(fresh_db, "fail@example.com")
        original_tier = fresh_db.get_user_by_id(uid)['tier']

        call_count = {'n': 0}
        real_activate = _udb.activate_subscription

        def _flaky_activate(user_id, tier='premium', days=30, payment_id=None, _commit=True):
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise RuntimeError("simulated DB blip")
            return real_activate(user_id, tier=tier, days=days,
                                 payment_id=payment_id, _commit=_commit)

        monkeypatch.setattr(_udb, 'activate_subscription', _flaky_activate)

        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREMIUM': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})):
            data = {
                'id': 'txn_fail',
                'customer_id': 'ctm_fail',
                'subscription_id': 'sub_fail',
                'custom_data': {'user_id': uid},
                'items': [{'price': {'id': 'pri_PREMIUM'}}],
            }

            with pytest.raises(RuntimeError):
                br._handle_transaction_completed(data, event_id='evt_fail_1')

        # Tier NOT flipped
        assert fresh_db.get_user_by_id(uid)['tier'] == original_tier

        # Marker NOT set
        conn = fresh_db._get_conn()
        marker = conn.execute(
            "SELECT 1 FROM processed_payments WHERE payment_id = ?",
            ('evt_fail_1',),
        ).fetchone()
        assert marker is None

        # Retry succeeds
        with patch.object(cfg_mod.Config, 'paddle_price_to_tier',
                          staticmethod(lambda: {'pri_PREMIUM': 'premium'})), \
             patch.object(cfg_mod.Config, 'paddle_price_to_credits',
                          staticmethod(lambda: {})):
            br._handle_transaction_completed(data, event_id='evt_fail_1')

        assert fresh_db.get_user_by_id(uid)['tier'] == 'premium'
        marker = conn.execute(
            "SELECT 1 FROM processed_payments WHERE payment_id = ?",
            ('evt_fail_1',),
        ).fetchone()
        assert marker is not None


# Empty event_id coverage now lives in
# tests/fastapi/test_billing_paddle.py::test_missing_event_id_returns_400
# (the Paddle webhook is served by FastAPI).
