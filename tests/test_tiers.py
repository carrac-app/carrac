"""Tests for the tier system — gates, monthly caps, config resolution, overrides."""

import re
from pathlib import Path
from unittest.mock import patch
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_tier_cache():
    """Reset tier config cache AND DB-level overrides between tests.

    The user_db singleton persists to a DB file that outlives a single test,
    so a mutator test (set_tier_config / set_user_overrides) leaves state
    that bleeds into later tests. Under pytest-xdist the test ordering is
    non-deterministic, which turns that bleed into flaky failures.

    Also clear the per-user overrides cache (module-level dict,
    keyed by user_id, TTL 60s). The teardown's raw ``DELETE FROM
    user_tier_overrides`` bypasses set_user_overrides/clear_user_overrides
    (the only write paths that call invalidate_user_overrides_cache), so
    without an explicit clear here a cached entry from one test could serve
    stale data to a later test that shares the same user_id within the TTL.
    """
    from carrac.auth import decorators as dec
    from user_db import user_db
    dec.invalidate_tier_config_cache()
    dec._user_overrides_cache.clear()
    yield
    try:
        with user_db._get_conn() as conn:
            conn.execute("DELETE FROM tier_config")
            conn.execute("DELETE FROM user_tier_overrides")
            conn.commit()
    except Exception:
        pass
    dec.invalidate_tier_config_cache()
    dec._user_overrides_cache.clear()


def _set_tier(client, tier):
    with client.session_transaction() as sess:
        sess['tier'] = tier


# ── Tier Limits Resolution ──

class TestTierLimits:

    def test_free_tier_defaults(self, app):
        from carrac.auth.decorators import TIER_LIMITS
        free = TIER_LIMITS['free']
        assert free['ai_queries_monthly'] == 0
        assert free['max_scenarios'] == 0
        assert free['cross_device_sync'] is False
        assert free['context_composer'] is False
        assert free['backtester_save'] is False
        assert free['topups_allowed'] is False
        assert free['data_export'] is False
        assert free['max_chart_panes'] == 4
        # Paid-tier launch matrix.
        assert free['max_plays'] == 3
        assert free['journal_trades_monthly'] == 20
        assert free['webhooks'] is False
        assert free['journal_analytics'] is False

    def test_member_tier_defaults(self, app):
        from carrac.auth.decorators import TIER_LIMITS
        member = TIER_LIMITS['member']
        assert member['ai_queries_monthly'] == 40
        assert member['max_scenarios'] == 15
        assert member['cross_device_sync'] is True
        assert member['context_composer'] is True
        assert member['backtester_save'] is True
        assert member['topups_allowed'] is True
        assert member['max_chart_panes'] == 4
        assert member['personal_report'] == 'standard'
        assert member['weekly_review'] is False
        # Paid-tier launch matrix.
        assert member['max_plays'] == 999
        assert member['max_alerts'] == 100
        assert member['journal_trades_monthly'] == 999
        assert member['webhooks'] is True
        assert member['journal_analytics'] is True

    def test_premium_tier_defaults(self, app):
        from carrac.auth.decorators import TIER_LIMITS
        premium = TIER_LIMITS['premium']
        assert premium['ai_queries_monthly'] == 150
        assert premium['max_scenarios'] == 999
        assert premium['personal_report'] == 'full'
        assert premium['weekly_review'] is True
        assert premium['tts'] is True
        assert premium['custom_recipes'] is True
        # Premium keeps the paid-launch matrix (inherited via
        # _PAID_LIMITS) plus its existing premium-only extras.
        assert premium['journal_trades_monthly'] == 999
        assert premium['webhooks'] is True
        assert premium['journal_analytics'] is True

    def test_tester_mirrors_member(self, app):
        from carrac.auth.decorators import TIER_LIMITS
        member = TIER_LIMITS['member']
        tester = TIER_LIMITS['tester']
        # quant + backtester are member+;
        # custom formulas remain the only premium-exclusive of this trio.
        premium_only_keys = {'custom_formulas'}
        member_included_keys = {'quant_engine', 'backtester'}
        for key in member:
            if key in premium_only_keys:
                assert tester[key] is True, f"tester.{key} should be True (premium feature)"
                assert member[key] is False, f"member.{key} should be False"
            elif key in member_included_keys:
                assert member[key] is True, f"member.{key} should be True (entry-tier differentiator)"
                assert tester[key] is True, f"tester.{key} should be True"
            else:
                # Tester (admin-granted) mirrors member's launch
                # matrix exactly, including max_plays/max_alerts.
                assert tester[key] == member[key], f"tester.{key} != member.{key}"

    def test_paid_tiers_set(self, app):
        from carrac.auth.decorators import PAID_TIERS
        assert 'member' in PAID_TIERS
        assert 'tester' in PAID_TIERS
        assert 'premium' in PAID_TIERS
        assert 'admin' in PAID_TIERS
        assert 'free' not in PAID_TIERS


# ── AI Gate Tests ──

class TestAIGates:

    # Tier gates for /api/ai/chat and /api/ai/weekly-review
    # are tested against the FastAPI handler in tests/fastapi/test_ai_chat.py
    # and tests/fastapi/test_ai_aux.py.

    def test_free_blocked_from_pattern_match(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.post('/api/ai/pattern-match', json={'asset': 'BTC'})
        assert resp.status_code == 403
        data = resp.get_json()
        assert data['upgrade_required'] is True


# ── Scenario Gate Tests (DEPRECATED — Scenarios blueprint removed) ──


# ── Sync Gate Tests ──

class TestSyncGates:

    def test_free_sync_poll_locked(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.get('/api/sync/poll?since=0')
        data = resp.get_json()
        assert data.get('tier_locked') is True

    def test_member_sync_poll_works(self, auth_client):
        _set_tier(auth_client, 'member')
        resp = auth_client.get('/api/sync/poll?since=0')
        data = resp.get_json()
        assert 'tier_locked' not in data or data['tier_locked'] is not True

# ── Backtester Gate Tests ──

class TestBacktesterGates:

    def test_free_strategies_returns_empty(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.get('/api/strategies')
        assert resp.status_code == 200
        assert resp.get_json()['strategies'] == []

    def test_free_cannot_save_strategy(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.post('/api/strategies', json={
            'name': 'Test', 'entry_formula': 'close > ema(20)',
            'exit_formula': 'close < ema(20)', 'direction': 'long'
        })
        assert resp.status_code == 403


# ── Export Gate Tests ──

class TestExportGates:

    def test_free_cannot_export_csv(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.get('/api/journal/export/csv')
        # 403 if journal blueprint registered, 404 if not (test env)
        assert resp.status_code in (403, 404)

    def test_free_cannot_export_json(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.get('/api/journal/export/json')
        assert resp.status_code in (403, 404)


# ── Journal Trade Monthly Cap Gate Tests ──
# journal_trades_monthly is enforced at SIX call sites that create a
# trade_journal row: the Flask create route (carrac/journal/routes.py — dead
# in the ASGI-strangled production deploy but still directly testable here), the
# FastAPI create route (fastapi_app/routers/journal.py — the live production path),
# the MCP trade_log tool (carrac/mcp/tools/journal.py), and the AI journal open/import routes
# (carrac/ai/routes.py) and the Flask drawing-import route (journal/routes.py,
# dormant behind the strangler shadow). All share the DB helper
# carrac.content_db.count_trades_this_month().

class TestJournalTradeCapGate:

    def _fresh_db(self, tmp_path, monkeypatch, name='journal_cap.db'):
        import carrac.db_pool as _cdb
        _cdb._db_type = None
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        from carrac.content_db import ContentDatabase
        return ContentDatabase(db_path=str(tmp_path / name))

    def test_count_trades_this_month_scoped_per_user(self, tmp_path, monkeypatch):
        db = self._fresh_db(tmp_path, monkeypatch)
        for _ in range(3):
            db.log_trade_open(user_id=1, asset='BTC', timeframe='1D',
                               direction='long', entry_price=100)
        db.log_trade_open(user_id=2, asset='ETH', timeframe='1D',
                           direction='long', entry_price=100)
        assert db.count_trades_this_month(1) == 3
        assert db.count_trades_this_month(2) == 1
        assert db.count_trades_this_month(999) == 0

    def test_count_trades_this_month_excludes_prior_months(self, tmp_path, monkeypatch):
        db = self._fresh_db(tmp_path, monkeypatch)
        tid = db.log_trade_open(user_id=1, asset='BTC', timeframe='1D',
                                 direction='long', entry_price=100)
        conn = db._get_conn()
        conn.execute(
            "UPDATE trade_journal SET opened_at = datetime('now', '-40 days') WHERE id = ?",
            (tid,)
        )
        conn.commit()
        assert db.count_trades_this_month(1) == 0

    def test_mcp_log_trade_blocks_free_tier_over_cap(self, tmp_path, monkeypatch):
        from carrac.mcp.runtime import set_mcp_caller, reset_mcp_caller
        from carrac.mcp.tools.journal import trade_log
        from carrac.mcp.protocol import ToolError
        import carrac.content_db as cdb_mod

        db = self._fresh_db(tmp_path, monkeypatch, name='mcp_cap.db')
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: db)
        for _ in range(20):
            db.log_trade_open(user_id=7, asset='BTC', timeframe='1D',
                               direction='long', entry_price=100)
        set_mcp_caller(7, 'free')
        try:
            with pytest.raises(ToolError, match='Monthly journal limit reached'):
                trade_log({'asset': 'BTC', 'entry_price': 100})
        finally:
            reset_mcp_caller()

    def test_mcp_log_trade_allows_under_cap(self, tmp_path, monkeypatch):
        from carrac.mcp.runtime import set_mcp_caller, reset_mcp_caller
        from carrac.mcp.tools.journal import trade_log
        import carrac.content_db as cdb_mod

        db = self._fresh_db(tmp_path, monkeypatch, name='mcp_cap_ok.db')
        monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: db)
        set_mcp_caller(8, 'free')
        try:
            result = trade_log({'asset': 'BTC', 'entry_price': 100})
            assert result['status'] == 'created'
        finally:
            reset_mcp_caller()


# ── Billing Gate Tests ──

class TestBillingGates:

    def test_free_cannot_create_payment(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.post('/api/billing/create-payment',
                                json={'amount_cents': 1000})
        assert resp.status_code in (403, 404)  # 404 if billing bp not in test app


# ── Context Composer Gate Tests ──

class TestComposerGates:

    def test_free_cannot_create_recipe(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.post('/api/ai/recipes', json={
            'name': 'Test', 'blocks': [{'id': 'price', 'enabled': True}]
        })
        assert resp.status_code == 403

    def test_member_can_clone_template(self, auth_client):
        _set_tier(auth_client, 'member')
        # context_composer reads and writes through db_pool, whose SQLite file
        # (carrac.db) is not the one user_db builds its schema in, so the
        # table has to exist there for the insert to land.
        from carrac.db_pool import get_conn
        with get_conn() as conn:
            conn.cursor().execute(
                'CREATE TABLE IF NOT EXISTS context_recipes ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, '
                'name TEXT NOT NULL, is_default BOOLEAN DEFAULT 0, '
                'blocks TEXT NOT NULL, created_at TIMESTAMP, updated_at TIMESTAMP)'
            )
            conn.commit()
        resp = auth_client.post('/api/ai/recipes', json={
            'name': 'From Template', 'template_id': 'quick_read'
        })
        assert resp.status_code == 201, resp.get_data(as_text=True)
        assert resp.get_json()['id']

    def test_member_cannot_create_custom_recipe(self, auth_client):
        _set_tier(auth_client, 'member')
        resp = auth_client.post('/api/ai/recipes', json={
            'name': 'Custom', 'blocks': [{'id': 'price', 'enabled': True}]
        })
        assert resp.status_code == 403
        assert 'Premium' in resp.get_json()['error']


# ── Tier Info API ──

class TestTierInfoAPI:

    def test_tier_info_returns_limits(self, auth_client):
        resp = auth_client.get('/api/tier-info')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['tier'] == 'member'
        assert data['max_chart_panes'] == 4
        assert data['ai_queries_monthly'] == 40
        assert data['cross_device_sync'] is True
        # The settings plan card reads these — they must mirror
        # TIER_LIMITS (enforcement), not hand-typed copy.
        assert data['max_plays'] == 999
        assert data['max_alerts'] == 100
        assert data['telegram'] is True
        assert data['discord'] is True
        # Paid-tier launch matrix.
        assert data['journal_trades_monthly'] == 999
        assert data['webhooks'] is True
        assert data['journal_analytics'] is True

    def test_free_tier_info(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.get('/api/tier-info')
        data = resp.get_json()
        assert data['tier'] == 'free'
        assert data['max_chart_panes'] == 4
        assert data['ai_queries_monthly'] == 0
        assert data['cross_device_sync'] is False
        assert data['max_plays'] == 3
        assert data['max_alerts'] == 5
        assert data['telegram'] is False
        assert data['discord'] is False
        # Paid-tier launch matrix.
        assert data['journal_trades_monthly'] == 20
        assert data['webhooks'] is False
        assert data['journal_analytics'] is False


class TestCapsCopyConsistency:
    """Displayed caps must not be hand-typed drift (found when the
    settings plan card told a free user 'Plays 3' while enforcement is 0)."""

    def _read(self, relpath):
        import pathlib
        return (pathlib.Path(__file__).parent.parent / relpath).read_text()

    def test_settings_card_does_not_hardcode_caps(self):
        src = self._read('templates/settings.html')
        assert "? '10' : '3'" not in src, "feat-plays must come from /api/tier-info, not a ternary"
        assert "? '100' : '5'" not in src, "feat-alerts must come from /api/tier-info, not a ternary"
        assert '/api/tier-info' in src

    def test_admin_reset_modal_email_attr_not_tojson(self):
        # |tojson emits raw double quotes inside a quoted HTML attribute,
        # truncating data-email to '' — the reset modal showed no email.
        src = self._read('templates/admin_users.html')
        assert 'data-email="{{ user.email|tojson }}"' not in src
        assert 'data-email="{{ user.email }}"' in src

    def test_lockout_warning_only_on_last_attempt(self):
        # First typo must not threaten lockout; warn only when 1 attempt left.
        src = self._read('carrac/auth/routes.py')
        assert '0 < remaining <= 2' not in src
        assert 'remaining == 1' in src


# ── Morning Report Access ──

class TestMorningReportAccess:

    def test_free_can_access_morning_report_page(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.get('/report/morning')
        assert resp.status_code != 301
        if resp.status_code == 302:
            assert '/premium' not in resp.headers.get('Location', '')

    def test_non_price_alert_does_not_500(self, auth_client):
        """Non-price alert types (regime_transition etc.) must not crash /report/morning.

        Pins no TypeError: unsupported format string passed to
        Undefined.__format__ when a.price is Undefined inside the template loop.
        """
        non_price_alerts = [
            {'id': 1, 'symbol': 'BTC', 'condition': 'regime_transition',
             'note': 'test', 'active': True, 'triggered': False},
            {'id': 2, 'symbol': 'SPX', 'condition': 'trendline_break',
             'note': None, 'active': True, 'triggered': False},
        ]
        with patch('carrac.reports.routes.push_notifications') as mock_pn:
            mock_pn.get_user_alerts.return_value = non_price_alerts
            resp = auth_client.get('/report/morning')
        assert resp.status_code == 200


# ── 3-Layer Config Merge ──

class TestConfigMerge:

    def test_unknown_tier_falls_back_to_free(self, app):
        with app.app_context():
            from carrac.auth.decorators import get_tier_limits, TIER_LIMITS
            limits = get_tier_limits('nonexistent_tier')
            assert limits['ai_queries_monthly'] == TIER_LIMITS['free']['ai_queries_monthly']
            assert limits['max_scenarios'] == TIER_LIMITS['free']['max_scenarios']

    def test_db_override_merges_into_hardcoded(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            # Set DB override: member gets 60 AI queries instead of 40
            user_db.set_tier_config('member', {'ai_queries_monthly': 60}, admin_id=None)
            dec.invalidate_tier_config_cache()
            limits = dec.get_tier_limits('member')
            assert limits['ai_queries_monthly'] == 60
            # Other keys should still come from hardcoded defaults
            assert limits['max_scenarios'] == 15
            assert limits['cross_device_sync'] is True

    def test_db_override_does_not_affect_other_tiers(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            user_db.set_tier_config('member', {'ai_queries_monthly': 60}, admin_id=None)
            dec.invalidate_tier_config_cache()
            # Premium should still have its own hardcoded value
            premium = dec.get_tier_limits('premium')
            assert premium['ai_queries_monthly'] == 150

    def test_per_user_override_wins_over_db_config(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            # DB tier config: member gets 60
            user_db.set_tier_config('member', {'ai_queries_monthly': 60}, admin_id=None)
            dec.invalidate_tier_config_cache()
            # Per-user override: this user gets 200
            user_db.set_user_overrides(1, {'ai_queries_monthly': 200}, admin_id=None, reason='beta tester')
            limits = dec.get_tier_limits_for_user(1, 'member')
            assert limits['ai_queries_monthly'] == 200

    def test_clear_user_overrides_reverts_to_tier_defaults(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            user_db.set_user_overrides(1, {'max_scenarios': 999}, admin_id=None, reason='test')
            limits_with = dec.get_tier_limits_for_user(1, 'member')
            assert limits_with['max_scenarios'] == 999
            user_db.clear_user_overrides(1)
            limits_without = dec.get_tier_limits_for_user(1, 'member')
            assert limits_without['max_scenarios'] == 15


# ── Cache Invalidation ──

class TestCacheInvalidation:

    def test_invalidation_forces_db_reread(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            # First read — populates cache
            dec.invalidate_tier_config_cache()
            dec.get_tier_limits('member')
            # Set DB config to a value distinct from the hardcoded default
            user_db.set_tier_config('member', {'max_alerts': 75}, admin_id=None)
            # Without invalidation — cache still has hardcoded default (Member baseline = 100)
            cached = dec.get_tier_limits('member')
            assert cached['max_alerts'] == 100  # hardcoded default, DB not re-read
            # Invalidate and re-read — DB override kicks in
            dec.invalidate_tier_config_cache()
            fresh = dec.get_tier_limits('member')
            assert fresh['max_alerts'] == 75

    def test_cache_ttl_prevents_db_reads(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            dec.invalidate_tier_config_cache()
            dec.get_tier_limits('member')  # Populates cache
            ts_after = dec._tier_config_ts
            # Immediately call again — should NOT refresh (within TTL)
            dec.get_tier_limits('member')
            assert dec._tier_config_ts == ts_after  # Timestamp unchanged


# ── Per-User Overrides Cache ──
# get_tier_limits() called user_db.get_user_overrides(uid) — an uncached
# Postgres round-trip + commit — on EVERY authed request. These tests cover
# the ~60s TTL cache added around that lookup.

class TestUserOverridesCache:

    def test_second_call_within_ttl_skips_db(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            original = user_db.get_user_overrides
            with patch.object(user_db, 'get_user_overrides', side_effect=original) as spy:
                dec._get_cached_user_overrides(1)
                dec._get_cached_user_overrides(1)
                assert spy.call_count == 1

    def test_none_result_is_cached_too(self, app):
        """The common case (no overrides for this user) must also short-circuit the DB."""
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            original = user_db.get_user_overrides
            with patch.object(user_db, 'get_user_overrides', side_effect=original) as spy:
                first = dec._get_cached_user_overrides(42)
                second = dec._get_cached_user_overrides(42)
                assert first is None
                assert second is None
                assert spy.call_count == 1

    def test_expiry_after_ttl_requeries(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            original = user_db.get_user_overrides
            with patch.object(user_db, 'get_user_overrides', side_effect=original) as spy:
                dec._get_cached_user_overrides(1)
                assert spy.call_count == 1
                # Backdate the cache entry past the TTL instead of sleeping
                ts, val = dec._user_overrides_cache[1]
                dec._user_overrides_cache[1] = (ts - dec._USER_OVERRIDES_TTL - 1, val)
                dec._get_cached_user_overrides(1)
                assert spy.call_count == 2

    def test_invalidate_forces_requery(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            original = user_db.get_user_overrides
            with patch.object(user_db, 'get_user_overrides', side_effect=original) as spy:
                dec._get_cached_user_overrides(1)
                dec._get_cached_user_overrides(1)
                assert spy.call_count == 1  # still cached
                dec.invalidate_user_overrides_cache(1)
                dec._get_cached_user_overrides(1)
                assert spy.call_count == 2  # forced re-query

    def test_set_user_overrides_invalidates_cache(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            # Prime the cache with the "no overrides yet" result
            assert dec._get_cached_user_overrides(1) is None
            assert 1 in dec._user_overrides_cache
            user_db.set_user_overrides(1, {'max_scenarios': 999}, admin_id=None, reason='test')
            # The write must have evicted the stale cache entry
            assert 1 not in dec._user_overrides_cache
            fresh = dec._get_cached_user_overrides(1)
            assert fresh['max_scenarios'] == 999

    def test_clear_user_overrides_invalidates_cache(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            user_db.set_user_overrides(1, {'max_scenarios': 999}, admin_id=None, reason='test')
            # Populate the cache with the override
            assert dec._get_cached_user_overrides(1)['max_scenarios'] == 999
            user_db.clear_user_overrides(1)
            assert 1 not in dec._user_overrides_cache
            assert dec._get_cached_user_overrides(1) is None

    def test_get_tier_limits_uses_cache_for_session_user(self, app):
        """End-to-end: get_tier_limits() (the hot-path caller) also benefits."""
        # Drive get_tier_limits() through an actual request context so
        # ``session.get('user_id')`` resolves the same way it does in production.
        with app.test_request_context():
            from flask import session as flask_session
            flask_session['user_id'] = 1
            from carrac.auth import decorators as dec
            from user_db import user_db
            original = user_db.get_user_overrides
            with patch.object(user_db, 'get_user_overrides', side_effect=original) as spy:
                dec.get_tier_limits('member')
                dec.get_tier_limits('member')
                assert spy.call_count == 1

    def test_cached_dict_not_mutated_by_reason_pop(self, app):
        """get_tier_limits pops `_reason` — must not corrupt the shared cache entry."""
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            user_db.set_user_overrides(1, {'max_scenarios': 999}, admin_id=None, reason='keep me')
            with app.test_request_context():
                from flask import session as flask_session
                flask_session['user_id'] = 1
                dec.get_tier_limits('member')
            # The cached entry itself must still carry `_reason` — only a
            # copy handed to the caller should have been popped.
            cached = dec._user_overrides_cache[1][1]
            assert cached.get('_reason') == 'keep me'


# ── Frozenset Normalization ──

class TestBlockedTfsNormalization:

    def test_blocked_tfs_is_frozenset_after_db_merge(self, app):
        with app.app_context():
            from carrac.auth import decorators as dec
            from user_db import user_db
            # Save a list (simulating JSON roundtrip) as blocked_tfs override
            user_db.set_tier_config('free', {'blocked_tfs': ['1m', '5m']}, admin_id=None)
            dec.invalidate_tier_config_cache()
            limits = dec.get_tier_limits('free')
            assert isinstance(limits['blocked_tfs'], frozenset)
            assert '1m' in limits['blocked_tfs']
            assert '5m' in limits['blocked_tfs']

    def test_blocked_tfs_default_is_frozenset(self, app):
        with app.app_context():
            from carrac.auth.decorators import get_tier_limits
            limits = get_tier_limits('free')
            assert isinstance(limits['blocked_tfs'], frozenset)
            limits_paid = get_tier_limits('member')
            assert isinstance(limits_paid['blocked_tfs'], frozenset)


# ── Admin Config Validation ──

class TestAdminConfigValidation:

    def test_admin_rejects_unknown_keys(self, admin_client, app):
        with app.app_context():
            resp = admin_client.post('/admin/tiers/member', data={
                'ai_queries_monthly': '60',
                'unknown_key_xyz': '123',
            })
            assert resp.status_code == 302  # Redirect back

    def test_admin_rejects_negative_int(self, admin_client, app):
        with app.app_context():
            resp = admin_client.post('/admin/tiers/member', data={
                'max_scenarios': '-5',
            })
            assert resp.status_code == 302

    def test_admin_rejects_invalid_tier(self, admin_client, app):
        with app.app_context():
            resp = admin_client.post('/admin/tiers/admin', data={
                'ai_queries_monthly': '9999',
            })
            assert resp.status_code == 302

    def test_admin_rejects_string_for_int_field(self, admin_client, app):
        with app.app_context():
            resp = admin_client.post('/admin/tiers/member', data={
                'max_scenarios': 'unlimited',
            })
            assert resp.status_code == 302


# ── Upgrade Required Flag ──

class TestUpgradeRequiredFlag:

    def test_tier_required_decorator_returns_upgrade_required(self, auth_client):
        _set_tier(auth_client, 'free')
        # Pattern-match still has a Flask-side tier gate; chat moved to FastAPI.
        resp = auth_client.post('/api/ai/pattern-match', json={'asset': 'BTC'})
        if resp.status_code == 403:
            data = resp.get_json()
            assert data.get('upgrade_required') is True

# ── carrac.tiers canonical constants (single source of truth) ──

class TestTiersModule:
    """Membership locks for carrac.tiers.

    If any assertion fails, a tier was silently added/removed from a set that
    other modules depend on.  Change tiers.py intentionally and update the
    expected sets here to match.
    """

    def test_paid_tiers_membership(self):
        from carrac.tiers import PAID_TIERS
        assert PAID_TIERS == frozenset({"member", "tester", "premium", "admin"})

    def test_tester_or_higher_membership(self):
        from carrac.tiers import TESTER_OR_HIGHER
        assert TESTER_OR_HIGHER == frozenset({"tester", "premium", "admin"})

    def test_premium_tiers_membership(self):
        from carrac.tiers import PREMIUM_TIERS
        assert PREMIUM_TIERS == frozenset({"premium", "admin"})

    def test_api_allowed_tiers_membership(self):
        from carrac.tiers import API_ALLOWED_TIERS
        assert API_ALLOWED_TIERS == frozenset({"free", "member", "premium", "tester", "admin"})

    def test_all_tiers_membership(self):
        from carrac.tiers import ALL_TIERS
        assert ALL_TIERS == frozenset({"free", "member", "tester", "premium", "admin"})

    def test_configurable_tiers_excludes_admin(self):
        from carrac.tiers import CONFIGURABLE_TIERS
        assert CONFIGURABLE_TIERS == frozenset({"free", "member", "tester", "premium"})
        assert "admin" not in CONFIGURABLE_TIERS

    def test_assignable_tiers_excludes_admin(self):
        from carrac.tiers import ASSIGNABLE_TIERS
        assert ASSIGNABLE_TIERS == frozenset({"free", "member", "tester", "premium"})
        assert "admin" not in ASSIGNABLE_TIERS

    def test_all_exports_are_frozensets(self):
        from carrac.tiers import (
            PAID_TIERS, TESTER_OR_HIGHER, PREMIUM_TIERS,
            API_ALLOWED_TIERS, ALL_TIERS, CONFIGURABLE_TIERS, ASSIGNABLE_TIERS,
        )
        for name, obj in [
            ("PAID_TIERS", PAID_TIERS),
            ("TESTER_OR_HIGHER", TESTER_OR_HIGHER),
            ("PREMIUM_TIERS", PREMIUM_TIERS),
            ("API_ALLOWED_TIERS", API_ALLOWED_TIERS),
            ("ALL_TIERS", ALL_TIERS),
            ("CONFIGURABLE_TIERS", CONFIGURABLE_TIERS),
            ("ASSIGNABLE_TIERS", ASSIGNABLE_TIERS),
        ]:
            assert isinstance(obj, frozenset), f"{name} should be a frozenset"

    def test_subset_relationships(self):
        from carrac.tiers import (
            PAID_TIERS, TESTER_OR_HIGHER, PREMIUM_TIERS,
            API_ALLOWED_TIERS, ALL_TIERS, CONFIGURABLE_TIERS, ASSIGNABLE_TIERS,
        )
        assert PAID_TIERS <= ALL_TIERS
        assert TESTER_OR_HIGHER <= PAID_TIERS
        assert PREMIUM_TIERS <= TESTER_OR_HIGHER
        assert ALL_TIERS <= API_ALLOWED_TIERS
        assert CONFIGURABLE_TIERS == ASSIGNABLE_TIERS
        assert CONFIGURABLE_TIERS <= ALL_TIERS

    def test_consumers_share_same_objects(self):
        """Consumer imports must be the exact same frozenset objects, not copies."""
        from carrac.tiers import PAID_TIERS, TESTER_OR_HIGHER, API_ALLOWED_TIERS
        from carrac.auth.decorators import PAID_TIERS as dec_pt
        from carrac.fastapi_app.deps.auth import PAID_TIERS as fa_pt
        from carrac.fastapi_app.deps.auth import TESTER_OR_HIGHER as fa_toh
        from carrac.rate_pools import API_ALLOWED_TIERS as rp_aat

        assert dec_pt is PAID_TIERS, "decorators.py PAID_TIERS must be the tiers.py object"
        assert fa_pt is PAID_TIERS, "deps/auth.py PAID_TIERS must be the tiers.py object"
        assert fa_toh is TESTER_OR_HIGHER, "deps/auth.py TESTER_OR_HIGHER must be the tiers.py object"
        assert rp_aat is API_ALLOWED_TIERS, "rate_pools.py API_ALLOWED_TIERS must be the tiers.py object"


class TestAiJournalRouteCapGate:
    """The AI journal open/import routes must enforce
    journal_trades_monthly."""

    def _mock_db(self, count):
        from unittest.mock import MagicMock
        db = MagicMock()
        db.count_trades_this_month.return_value = count
        db.drawing_list.return_value = []
        return db

    def test_ai_journal_open_blocked_at_cap_for_free(self, auth_client, monkeypatch):
        _set_tier(auth_client, 'free')
        import carrac.content_db as cdb
        db = self._mock_db(count=20)
        monkeypatch.setattr(cdb, 'get_content_db', lambda: db)
        resp = auth_client.post('/api/ai/journal/open',
                                json={'asset': 'BTC', 'entry_price': 100})
        assert resp.status_code == 403
        assert resp.get_json()['limit_type'] == 'journal_trade'
        db.log_trade_open.assert_not_called()

    def test_ai_journal_open_allowed_below_cap_for_free(self, auth_client, monkeypatch):
        _set_tier(auth_client, 'free')
        import carrac.content_db as cdb
        db = self._mock_db(count=3)
        db.log_trade_open.return_value = 1
        monkeypatch.setattr(cdb, 'get_content_db', lambda: db)
        resp = auth_client.post('/api/ai/journal/open',
                                json={'asset': 'BTC', 'entry_price': 100})
        assert resp.status_code == 200
        db.log_trade_open.assert_called_once()

    def test_ai_journal_import_reports_capped_for_free(self, auth_client, monkeypatch):
        _set_tier(auth_client, 'free')
        import carrac.content_db as cdb
        db = self._mock_db(count=20)
        db.drawing_list.return_value = [
            {'id': 1, 'drawing_type': 'long_position',
             'metadata': {'points': [{'value': 100}, {'value': 120}]},
             'asset': 'BTC', 'timeframe': '1D'},
        ]
        monkeypatch.setattr(cdb, 'get_content_db', lambda: db)
        resp = auth_client.post('/api/ai/journal/import', json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['imported'] == 0
        assert data['capped'] is True
        db.log_trade_open.assert_not_called()


# ── quant_engine/backtester knob-driven gates ──
#
# TIER_LIMITS['member']['quant_engine'] and ['backtester'] are True (see
# TestTierLimits above), so the quant_bp Flask routes and the MCP quant tools
# must gate on that knob, not on a hardcoded tester/premium/admin list —
# otherwise a paying member is 403'd from the features sold as their tier's
# differentiator. These tests cover the
# carrac.auth.decorators.tier_feature_required() Flask decorator, the
# carrac.mcp.registry.tool_entitled()/`feature` knob mechanism, a
# representative quant_bp route, a representative backtest surface, every
# MCP tool gated this way, and that API-key access still works.

class TestTierFeatureRequiredDecorator:
    """Unit tests for tier_feature_required() — mirrors
    TestTesterOrKeyRequiredApiKeyTier (test_mcp_quota_and_auth.py), which
    covers the tester_or_key_required decorator that this one
    supplements (not replaces — tester_or_key_required is still used
    elsewhere)."""

    def _get_decorated_endpoint(self, feature='quant_engine'):
        from carrac.auth.decorators import tier_feature_required

        @tier_feature_required(feature)
        def _view():
            return ('OK', 200)
        return _view

    def test_free_session_blocked(self, app):
        view = self._get_decorated_endpoint()
        with app.test_request_context('/'):
            from flask import session as _session
            _session['user_id'] = 'u1'
            _session['tier'] = 'free'
            result = view()
        assert isinstance(result, tuple) and result[1] == 403

    def test_member_session_allowed(self, app):
        view = self._get_decorated_endpoint()
        with app.test_request_context('/'):
            from flask import session as _session
            _session['user_id'] = 'u1'
            _session['tier'] = 'member'
            result = view()
        assert result == ('OK', 200)

    def test_premium_session_allowed(self, app):
        view = self._get_decorated_endpoint()
        with app.test_request_context('/'):
            from flask import session as _session
            _session['user_id'] = 'u1'
            _session['tier'] = 'premium'
            result = view()
        assert result == ('OK', 200)

    def test_shared_admin_key_allowed(self, app):
        """Shared admin key (no g.api_key_tier) always passes — same as
        tester_or_key_required's admin/internal-key behavior."""
        view = self._get_decorated_endpoint()
        with app.test_request_context('/', headers={'X-Internal-Key': 'admin-key'}):
            with patch('carrac.auth.decorators.verify_api_key', return_value=True):
                result = view()
        assert result == ('OK', 200)

    def test_free_tier_api_key_blocked(self, app):
        """Per-user API key resolving to a free-tier user is denied.

        get_tier_limits_for_user() runs for real (not mocked): a nonexistent
        user_id has no DB overrides, so it resolves straight to hardcoded
        TIER_LIMITS['free'] — quant_engine/backtester are False there.
        """
        from flask import g

        view = self._get_decorated_endpoint()
        with app.test_request_context(
            '/', headers={'X-API-Key': 'cvx_k_fake', 'Content-Type': 'application/json'},
        ):
            def _verify_and_set(key):
                g.api_key_tier = 'free'
                g.api_key_user_id = 123456789  # nonexistent — no DB overrides
                return True

            with patch('carrac.auth.decorators.verify_api_key', side_effect=_verify_and_set):
                result = view()
        assert isinstance(result, tuple) and result[1] == 403

    def test_member_tier_api_key_allowed(self, app):
        """Per-user API key resolving to a member-tier user is now allowed
 — tier-resolved from the KEY's owning user, not the
        caller's own session. get_tier_limits_for_user() runs for real —
        see test_free_tier_api_key_blocked above."""
        from flask import g

        view = self._get_decorated_endpoint()
        with app.test_request_context(
            '/', headers={'X-API-Key': 'cvx_k_fake', 'Content-Type': 'application/json'},
        ):
            def _verify_and_set(key):
                g.api_key_tier = 'member'
                g.api_key_user_id = 123456790  # nonexistent — no DB overrides
                return True

            with patch('carrac.auth.decorators.verify_api_key', side_effect=_verify_and_set):
                result = view()
        assert result == ('OK', 200)


class TestQuantEngineRouteGate:
    """Representative quant_bp Flask route (19 routes
    moved from @tester_or_key_required to
    @tier_feature_required('quant_engine'))."""

    def test_free_blocked_from_quant_route(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.get('/api/quant/metrics')
        assert resp.status_code == 403
        assert resp.get_json().get('upgrade_required') is True

    def test_member_not_blocked_from_quant_route(self, auth_client):
        """Member passes the tier gate. The test DB has no real quant.*
        Postgres schema, so a non-403 response (typically 500 from the
        route's own DB-error handler) is what proves the gate let the
        request through — 403 would mean the gate is still wrong."""
        _set_tier(auth_client, 'member')
        resp = auth_client.get('/api/quant/metrics')
        assert resp.status_code != 403

    @pytest.mark.parametrize('tier', ['tester', 'premium', 'admin'])
    def test_other_paid_tiers_not_blocked(self, auth_client, tier):
        _set_tier(auth_client, tier)
        resp = auth_client.get('/api/quant/metrics')
        assert resp.status_code != 403

    def test_admin_key_bypasses_gate(self, client):
        resp = client.get('/api/quant/metrics',
                          headers={'X-Internal-Key': 'test-api-key-for-testing'})
        assert resp.status_code != 403


class TestBacktesterRouteAndToolGate:
    """Representative backtest surfaces: the Flask scanner-backtest route
    (wired to the `backtester` knob) and the MCP
    backtest_play/scan_and_backtest tools (min_tier='member',
    feature='backtester')."""

    def test_free_blocked_from_scanner_backtest_route(self, auth_client):
        _set_tier(auth_client, 'free')
        resp = auth_client.post('/api/scanner/backtest', json={'expression': 'RSI(14) < 30'})
        assert resp.status_code == 403

    def test_member_allowed_scanner_backtest_route(self, auth_client, monkeypatch):
        monkeypatch.setattr(
            'carrac.mcp.tools.backtest.scan_and_backtest',
            lambda args: {'results': [], 'count': 0},
        )
        _set_tier(auth_client, 'member')
        resp = auth_client.post('/api/scanner/backtest', json={'expression': 'RSI(14) < 30'})
        assert resp.status_code == 200

    def _dispatch(self, tool_name, tier, user_id=1):
        from carrac.mcp.protocol import dispatch
        from carrac.mcp.runtime import set_mcp_caller, reset_mcp_caller
        set_mcp_caller(user_id, tier)
        try:
            return dispatch({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': tool_name, 'arguments': {}},
            })
        finally:
            reset_mcp_caller()

    def test_free_denied_backtest_play_via_dispatch(self, monkeypatch):
        monkeypatch.setattr('carrac.mcp.registry.call_tool', lambda name, args: {'ok': True})
        result = self._dispatch('backtest_play', 'free')
        assert result['result'].get('isError') is True
        text = result['result']['content'][0]['text']
        assert 'member' in text.lower()

    def test_member_allowed_backtest_play_via_dispatch(self, monkeypatch):
        monkeypatch.setattr('carrac.mcp.registry.call_tool', lambda name, args: {'ok': True})
        result = self._dispatch('backtest_play', 'member')
        assert result['result'].get('isError') is not True


class TestFrontendTierFlagGates:
    """The layer the tier fix missed.

    The frontend has TWO tier flags with different tier sets:

        window._isPremium          = member, premium, admin, tester
        window._hasPremiumFeatures = premium, admin, tester   (NOT member)

    Both are legitimate — _hasPremiumFeatures is the correct, stricter bar for
    genuinely premium-only features (custom formulas, macro/quant chart
    series). Picking the wrong one makes the UI paywall a
    feature the backend already grants.

    This test asserts, for each known gate site, that the flag guarding it
    covers exactly the tiers TIER_LIMITS grants the underlying feature. Any
    future flag swap (either direction) or tier-matrix change breaks it.
    """

    # (source file, feature key in TIER_LIMITS, anchor literal to locate the gate)
    GATE_SITES = [
        ('static/js/backtest-ui.js', 'backtester',
         'Strategy backtesting is available'),
        ('static/js/backtest-ui.js', 'backtester_save',
         'Save and load backtester strategies'),
        ('static/js/scenarios.js', 'backtester',
         'data-action="scanner-open-backtest"'),
        # Negative control: genuinely premium-only, must stay _hasPremiumFeatures.
        ('static/js/custom-indicators.js', 'custom_formulas',
         'Custom indicators and formulas are available'),
    ]

    FLAG_DEF_RE = re.compile(
        r"window\.(_isPremium|_hasPremiumFeatures)\s*=\s*\[([^\]]*)\]"
        r"\.includes\(window\._userTier\)"
    )

    @staticmethod
    def _flag_tier_sets():
        """Parse both flag definitions out of templates/index.html."""
        src = (_REPO_ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')
        found = {
            name: {t.strip().strip('\'"') for t in tiers.split(',') if t.strip()}
            for name, tiers in TestFrontendTierFlagGates.FLAG_DEF_RE.findall(src)
        }
        assert set(found) == {'_isPremium', '_hasPremiumFeatures'}, (
            f'expected both tier flags defined in templates/index.html, got {sorted(found)}'
        )
        return found

    @staticmethod
    def _gating_flag(rel_path, anchor):
        """Return the flag name guarding the code at ``anchor``.

        Scans backwards from the anchor for the nearest window._isPremium /
        window._hasPremiumFeatures reference — how the gate reads to a human.
        """
        src = (_REPO_ROOT / rel_path).read_text(encoding='utf-8')
        idx = src.find(anchor)
        assert idx != -1, f'anchor {anchor!r} not found in {rel_path} — gate site moved?'
        before = src[:idx]
        matches = list(re.finditer(r'window\.(_isPremium|_hasPremiumFeatures)\b', before))
        assert matches, f'no tier flag guards {anchor!r} in {rel_path} — gate removed?'
        return matches[-1].group(1)

    def test_flag_definitions_match_tier_limits(self):
        """The flags themselves must stay in sync with TIER_LIMITS."""
        from carrac.auth.decorators import TIER_LIMITS
        flags = self._flag_tier_sets()
        assert flags['_isPremium'] == {
            t for t in TIER_LIMITS if TIER_LIMITS[t].get('backtester')
        }
        assert flags['_hasPremiumFeatures'] == {
            t for t in TIER_LIMITS if TIER_LIMITS[t].get('custom_formulas')
        }

    def test_index_and_standalone_flag_definitions_agree(self):
        """chart_standalone.html defines the same two flags — they must not drift."""
        standalone = (_REPO_ROOT / 'templates' / 'chart_standalone.html').read_text(
            encoding='utf-8')
        found = {
            name: {t.strip().strip('\'"') for t in tiers.split(',') if t.strip()}
            for name, tiers in self.FLAG_DEF_RE.findall(standalone)
        }
        assert found == self._flag_tier_sets()

    @pytest.mark.parametrize('rel_path,feature,anchor', GATE_SITES)
    def test_gate_uses_flag_matching_backend_entitlement(self, rel_path, feature, anchor):
        from carrac.auth.decorators import TIER_LIMITS
        entitled = {t for t in TIER_LIMITS if TIER_LIMITS[t].get(feature)}
        flag = self._gating_flag(rel_path, anchor)
        flag_tiers = self._flag_tier_sets()[flag]
        assert flag_tiers == entitled, (
            f"{rel_path} gates {anchor!r} on window.{flag} (tiers {sorted(flag_tiers)}), "
            f"but TIER_LIMITS grants '{feature}' to {sorted(entitled)}. "
            f"Wrong flag."
        )


class TestMcpToolsFeatureGate:
    """Every MCP tool moved off a hardcoded premium min_tier,
    verified via the actual TOOL_REGISTRY entries + tool_entitled() — free
    denied, member (and up) allowed. indicator_run_formula is included as a
    negative control: it is genuinely premium-only (the custom formula
    engine — TIER_LIMITS['custom_formulas'] is False for member) and must
    NOT have been touched."""

    # create_quant_alert/list_quant_alerts/list_regime_alerts were
    # absorbed into alert_manage, which is NOT in this list — its own
    # registry entry has feature=None (min_tier='member' only) because most
    # of its alert_types (price/indicator/formula) need no extra knob; the
    # quant_engine gate for the absorbed quant alert_types is now enforced
    # inside the handler (alerts.py:_require_quant_engine), not declaratively
    # via the registry — see test_quant_engine_bundle.py for that coverage.
    QUANT_ENGINE_TOOLS = [
        'quant_get_regime', 'quant_get_leadlag_ranking', 'quant_get_transmission_chains',
        'quant_get_analogues', 'quant_get_leadlag_pair', 'quant_get_chain_history',
        'quant_get_anomalies', 'quant_get_cot',
        'quant_get_regime_history', 'alert_create_regime_watch',
        'quant_get_data', 'quant_get_correlations', 'backtest_get_historical_analog',
    ]
    # backtest_play/scan_and_backtest/backtest_indicator_formula
    # consolidated into backtest_run (source=play|scan|formula).
    BACKTESTER_TOOLS = ['backtest_run']
    # list_backtest_runs/get_backtest_run consolidated into
    # backtest_get_runs (run_id absent/present).
    BACKTESTER_SAVE_TOOLS = ['backtest_save_run', 'backtest_get_runs']

    @pytest.fixture(scope='class', autouse=True)
    @classmethod
    def _load_registry(cls):
        import carrac.mcp.tools  # noqa: F401 — triggers @mcp_tool registration

    @pytest.fixture(scope='class')
    @classmethod
    def registry(cls):
        from carrac.mcp.registry import TOOL_REGISTRY
        return TOOL_REGISTRY

    @pytest.mark.parametrize('name', QUANT_ENGINE_TOOLS)
    def test_quant_engine_tools_registered_with_feature(self, registry, name):
        assert registry[name]['min_tier'] == 'member'
        assert registry[name]['feature'] == 'quant_engine'

    @pytest.mark.parametrize('name', BACKTESTER_TOOLS)
    def test_backtester_tools_registered_with_feature(self, registry, name):
        assert registry[name]['min_tier'] == 'member'
        assert registry[name]['feature'] == 'backtester'

    @pytest.mark.parametrize('name', BACKTESTER_SAVE_TOOLS)
    def test_backtester_save_tools_registered_with_feature(self, registry, name):
        assert registry[name]['min_tier'] == 'member'
        assert registry[name]['feature'] == 'backtester_save'

    @pytest.mark.parametrize(
        'name', QUANT_ENGINE_TOOLS + BACKTESTER_TOOLS + BACKTESTER_SAVE_TOOLS,
    )
    def test_free_denied_every_fixed_tool(self, registry, name):
        from carrac.mcp.registry import tool_entitled
        reg = registry[name]
        assert not tool_entitled('free', reg['min_tier'], reg['feature']), (
            f'free should NOT be entitled to {name}'
        )

    @pytest.mark.parametrize(
        'name', QUANT_ENGINE_TOOLS + BACKTESTER_TOOLS + BACKTESTER_SAVE_TOOLS,
    )
    @pytest.mark.parametrize('tier', ['member', 'tester', 'premium', 'admin'])
    def test_paid_tiers_allowed_every_fixed_tool(self, registry, name, tier):
        from carrac.mcp.registry import tool_entitled
        reg = registry[name]
        assert tool_entitled(tier, reg['min_tier'], reg['feature']), (
            f'{tier} should be entitled to {name}'
        )

    def test_run_custom_formula_untouched_control(self, registry):
        """Negative control: indicator_run_formula IS the custom formula
        engine and must remain premium-only, unaffected by that change."""
        assert registry['indicator_run_formula']['min_tier'] == 'premium'
        assert registry['indicator_run_formula'].get('feature') is None

    def test_all_expected_tool_names_still_present(self, registry):
        """Guards against a typo silently dropping a tool from the sweep."""
        all_names = (self.QUANT_ENGINE_TOOLS + self.BACKTESTER_TOOLS
                     + self.BACKTESTER_SAVE_TOOLS)
        assert len(all_names) == 16
        for name in all_names:
            assert name in registry, f'{name} missing from TOOL_REGISTRY'

    def test_absorbed_tools_no_longer_standalone(self, registry):
        """These old names must NOT be separate registry entries
        anymore — they're plain helpers, still reachable via the legacy
        alias for backward compat (see test_mcp_scope.py)."""
        absorbed = (
            'create_quant_alert', 'list_quant_alerts', 'list_regime_alerts',
            'backtest_play', 'scan_and_backtest', 'backtest_indicator_formula',
            'list_backtest_runs', 'get_backtest_run',
        )
        for name in absorbed:
            assert name not in registry, f'{name} should no longer be a standalone tool'
