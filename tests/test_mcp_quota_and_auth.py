"""Tests for MCP play/scenario quota enforcement (Fix 1) and
tester_or_key_required tier check for API keys (Fix 2)."""

import pytest

from carrac.mcp import tools  # noqa: F401 — registers tools
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError


# ── Shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _bind_user(bind_mcp_user):
    """Bind default MCP user_id=42 for all tests in this file."""
    return bind_mcp_user


# =============================================================================
# Fix 1 — MCP play_create quota bypass
# =============================================================================

class TestCreatePlayQuota:
    """play_create MCP tool must enforce max_plays before inserting."""

    def _handler(self):
        return TOOL_REGISTRY['play_create']['handler']

    def _patch_quota(self, monkeypatch, current, max_plays, tier='free'):
        """Patch count_active_plays, get_mcp_caller_tier, and get_tier_limits_for_user
        at the source modules that the tool imports from locally."""
        import carrac.plays.models as pm
        import carrac.mcp.runtime as rt
        import carrac.auth.decorators as dec

        monkeypatch.setattr(pm, 'count_active_plays', lambda uid: current)
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: tier)
        monkeypatch.setattr(
            dec, 'get_tier_limits_for_user',
            lambda uid, t: {'max_plays': max_plays},
        )

    def test_free_tier_at_limit_raises(self, app, monkeypatch):
        """Free tier with max_plays=0 and count=0 → quota hit, ToolError raised."""
        self._patch_quota(monkeypatch, current=0, max_plays=0, tier='free')

        fn = self._handler()
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({'title': 'Test play'})
        assert 'Play limit reached' in str(exc.value)
        assert '0/0' in str(exc.value)

    def test_member_tier_within_quota_succeeds(self, app, monkeypatch):
        """Member tier with count=4, max_plays=5 → allowed, _create called."""
        import carrac.plays.models as pm

        self._patch_quota(monkeypatch, current=4, max_plays=5, tier='member')

        captured = {}
        monkeypatch.setattr(pm, 'play_create', lambda **kw: captured.update(kw) or {'id': 99, **kw})

        fn = self._handler()
        with app.app_context():
            fn({'title': 'BTC Long'})
        # Should not raise; the captured dict will have the title
        assert captured.get('title') == 'BTC Long'

    def test_member_tier_at_exact_limit_raises(self, app, monkeypatch):
        """count == max_plays (boundary) → quota hit."""
        self._patch_quota(monkeypatch, current=5, max_plays=5, tier='member')

        fn = self._handler()
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({'title': 'Overflow play'})
        assert 'Play limit reached' in str(exc.value)
        assert '5/5' in str(exc.value)

    def test_error_message_contains_upgrade_hint(self, app, monkeypatch):
        """Error message must mention 'Upgrade your plan'."""
        self._patch_quota(monkeypatch, current=3, max_plays=3, tier='free')

        fn = self._handler()
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({'title': 'Play'})
        assert 'Upgrade your plan' in str(exc.value)


class TestCreateFormulaPlayQuota:
    """play_create_from_formula_alert MCP tool must also enforce max_plays."""

    def _handler(self):
        return TOOL_REGISTRY['play_create_from_formula_alert']['handler']

    def _patch_quota(self, monkeypatch, current, max_plays, tier='free'):
        import carrac.plays.models as pm
        import carrac.mcp.runtime as rt
        import carrac.auth.decorators as dec

        monkeypatch.setattr(pm, 'count_active_plays', lambda uid: current)
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: tier)
        monkeypatch.setattr(
            dec, 'get_tier_limits_for_user',
            lambda uid, t: {'max_plays': max_plays},
        )

    def test_quota_check_fires_before_create(self, app, monkeypatch):
        """When free tier at limit, ToolError raised before _create_play is called."""
        import push_notifications
        import carrac.plays.models as pm

        monkeypatch.setattr(
            push_notifications, 'get_user_alerts',
            lambda u, active_only=False: [
                {'id': 5, 'alert_type': 'formula', 'symbol': 'BTC',
                 'expression': 'RSI(14) < 30', 'timeframe': '1h'},
            ],
        )
        self._patch_quota(monkeypatch, current=0, max_plays=0, tier='free')

        create_called = []
        monkeypatch.setattr(pm, 'play_create', lambda **kw: create_called.append(kw) or {})

        fn = self._handler()
        with app.app_context():
            with pytest.raises(ToolError) as exc:
                fn({'alert_id': 5})
        assert 'Play limit reached' in str(exc.value)
        # _create_play must NOT have been called
        assert create_called == []

    def test_within_quota_proceeds(self, app, monkeypatch):
        """Premium tier with room → alert lookup proceeds normally."""
        import push_notifications
        import carrac.plays.models as pm

        monkeypatch.setattr(
            push_notifications, 'get_user_alerts',
            lambda u, active_only=False: [
                {'id': 7, 'alert_type': 'formula', 'symbol': 'ETH',
                 'expression': 'EMA(9) > EMA(21)', 'timeframe': '4h'},
            ],
        )
        self._patch_quota(monkeypatch, current=2, max_plays=25, tier='premium')

        captured = {}
        monkeypatch.setattr(pm, 'play_create', lambda **kw: captured.update(kw) or {'id': 10, **kw})

        fn = self._handler()
        with app.app_context():
            out = fn({'alert_id': 7})
        assert out['source_alert_id'] == 7
        assert captured.get('primary_asset') == 'ETH'


# =============================================================================
# Fix 2 — tester_or_key_required tier check for API keys
# =============================================================================

class TestTesterOrKeyRequiredApiKeyTier:
    """tester_or_key_required must gate per-user keys by tier."""

    def _get_decorated_endpoint(self):
        """Return a minimal view function wrapped with tester_or_key_required."""
        from carrac.auth.decorators import tester_or_key_required

        @tester_or_key_required
        def _view():
            return ('OK', 200)

        return _view

    def test_admin_internal_key_no_tier_allowed(self, app):
        """Shared admin key: verify_api_key returns True but g.api_key_tier is absent → allowed."""
        from unittest.mock import patch

        view = self._get_decorated_endpoint()

        with app.test_request_context('/', headers={'X-Internal-Key': 'admin-key'}):
            with patch('carrac.auth.decorators.verify_api_key', return_value=True):
                # Do NOT set g.api_key_tier — simulates the shared admin key path
                result = view()
        assert result == ('OK', 200)

    def test_premium_api_key_allowed(self, app):
        """Per-user premium key (g.api_key_tier='premium') → allowed."""
        from unittest.mock import patch
        from flask import g

        view = self._get_decorated_endpoint()

        with app.test_request_context('/', headers={'X-API-Key': 'cvx_k_fake'}):
            def _verify_and_set(key):
                g.api_key_tier = 'premium'
                return True

            with patch('carrac.auth.decorators.verify_api_key', side_effect=_verify_and_set):
                result = view()
        assert result == ('OK', 200)

    def test_tester_api_key_allowed(self, app):
        """Per-user tester key → allowed."""
        from unittest.mock import patch
        from flask import g

        view = self._get_decorated_endpoint()

        with app.test_request_context('/', headers={'X-API-Key': 'cvx_k_fake'}):
            def _verify_and_set(key):
                g.api_key_tier = 'tester'
                return True

            with patch('carrac.auth.decorators.verify_api_key', side_effect=_verify_and_set):
                result = view()
        assert result == ('OK', 200)

    def test_free_tier_api_key_blocked(self, app):
        """Per-user free key (g.api_key_tier='free') → 403."""
        from unittest.mock import patch
        from flask import g

        view = self._get_decorated_endpoint()

        with app.test_request_context(
            '/',
            headers={'X-API-Key': 'cvx_k_fake', 'Content-Type': 'application/json'},
        ):
            def _verify_and_set(key):
                g.api_key_tier = 'free'
                return True

            with patch('carrac.auth.decorators.verify_api_key', side_effect=_verify_and_set):
                result = view()
        # Should be a (response, 403) tuple or a Flask Response with status 403
        if isinstance(result, tuple):
            assert result[1] == 403
        else:
            assert result.status_code == 403

    def test_member_tier_api_key_blocked(self, app):
        """Per-user member key (g.api_key_tier='member') → 403 (not in allowed set)."""
        from unittest.mock import patch
        from flask import g

        view = self._get_decorated_endpoint()

        with app.test_request_context(
            '/',
            headers={'X-API-Key': 'cvx_k_fake', 'Content-Type': 'application/json'},
        ):
            def _verify_and_set(key):
                g.api_key_tier = 'member'
                return True

            with patch('carrac.auth.decorators.verify_api_key', side_effect=_verify_and_set):
                result = view()
        if isinstance(result, tuple):
            assert result[1] == 403
        else:
            assert result.status_code == 403

    def test_unauthenticated_json_gets_401(self, app):
        """No key, no session, JSON Accept → 401 (not a redirect)."""
        from unittest.mock import patch

        view = self._get_decorated_endpoint()

        with app.test_request_context(
            '/',
            headers={'Content-Type': 'application/json'},
        ):
            with patch('carrac.auth.decorators.verify_api_key', return_value=False):
                result = view()
        if isinstance(result, tuple):
            assert result[1] == 401
        else:
            assert result.status_code == 401
