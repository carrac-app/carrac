"""
API hardening tests.

Plays CRUD endpoints accept API key (auth_or_key_required)
Telegram token Fernet encryption — roundtrip + legacy + missing key warning
DNS rebinding fix — validate_outbound_url pins IP; news test-feed uses pinned IP
MCP tools all have explicit min_tier (no unset defaults)
Alerts blueprint _enforce_alerts_csrf before_request hook
Assets blueprint _enforce_assets_csrf before_request hook
"""

from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Telegram token encryption
# ---------------------------------------------------------------------------

class TestTelegramCrypto:
    """Roundtrip + legacy fallback + missing-key warning."""

    def _make_key(self):
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    def test_roundtrip_with_key(self):
        """Encrypt then decrypt returns the original token."""
        key = self._make_key()
        with patch.dict(os.environ, {'TELEGRAM_TOKEN_ENCRYPTION_KEY': key}):
            # Reset cached instance
            import carrac.secret_box as tc
            tc._fernet_instance = None
            tc._warned_missing_key = False

            plain = '123456789:AAFakeTestTokenXYZ-abc'
            encrypted = tc.encrypt_token(plain)
            assert encrypted.startswith('fernet:')
            assert encrypted != plain

            decrypted = tc.decrypt_token(encrypted)
            assert decrypted == plain

    def test_legacy_plaintext_passthrough(self):
        """Legacy plaintext tokens (no prefix) are returned unchanged."""
        key = self._make_key()
        with patch.dict(os.environ, {'TELEGRAM_TOKEN_ENCRYPTION_KEY': key}):
            import carrac.secret_box as tc
            tc._fernet_instance = None
            tc._warned_missing_key = False

            plain = '987654321:BBLegacyToken'
            # Stored without fernet: prefix (legacy)
            result = tc.decrypt_token(plain)
            assert result == plain

    def test_missing_key_logs_warning(self, caplog):
        """When env var is missing, encrypt_token returns plaintext and logs WARNING."""
        env = {k: v for k, v in os.environ.items() if k != 'TELEGRAM_TOKEN_ENCRYPTION_KEY'}
        with patch.dict(os.environ, env, clear=True):
            import carrac.secret_box as tc
            tc._fernet_instance = None
            tc._warned_missing_key = False

            plain = '111222333:CCMissingKeyToken'
            with caplog.at_level(logging.WARNING, logger='carrac.secret_box'):
                result = tc.encrypt_token(plain)

            assert result == plain  # Falls back to plaintext
            assert any('TELEGRAM_TOKEN_ENCRYPTION_KEY' in r.message for r in caplog.records)

    def test_empty_token_passthrough(self):
        """Empty/None tokens are returned unchanged."""
        import carrac.secret_box as tc
        assert tc.encrypt_token('') == ''
        assert tc.decrypt_token('') == ''


# ---------------------------------------------------------------------------
# DNS rebinding — validate_outbound_url returns pinned IP
# ---------------------------------------------------------------------------

class TestDnsRebinding:
    """validate_outbound_url must return a pinned IP on success."""

    def test_success_returns_pinned_ip(self):
        """When URL is safe, the second return value is the resolved IP string."""
        import socket
        from security import validate_outbound_url

        # Use a URL that resolves to a public IP (mock DNS to avoid real lookups)
        fake_ip = '93.184.216.34'  # example.com's IP

        with patch('socket.getaddrinfo') as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, '', (fake_ip, 0))
            ]
            ok, pinned = validate_outbound_url('https://example.com/feed')

        assert ok is True
        assert pinned == fake_ip

    def test_private_ip_rejected(self):
        """URLs resolving to RFC-1918 addresses are rejected."""
        import socket
        from security import validate_outbound_url

        with patch('socket.getaddrinfo') as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('10.0.0.1', 0))
            ]
            ok, reason = validate_outbound_url('https://evil.example.com/feed')

        assert ok is False
        assert 'private' in reason.lower() or 'internal' in reason.lower()

    def test_rebind_scenario_pinned(self):
        """Second call to getaddrinfo would return different IP — but caller uses pinned value."""
        import socket
        from security import validate_outbound_url

        # Simulate: first call returns safe public IP, second would return loopback
        # The function should return the pinned IP from the FIRST (only) resolution.
        safe_ip = '8.8.8.8'

        with patch('socket.getaddrinfo') as mock_gai:
            mock_gai.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 0, '', (safe_ip, 0))
            ]
            ok, pinned = validate_outbound_url('https://rebind.example.com/feed')

        assert ok is True
        assert pinned == safe_ip
        # Only one DNS resolution was made (the pinned one)
        assert mock_gai.call_count == 1


# ---------------------------------------------------------------------------
# MCP tools all have explicit min_tier
# ---------------------------------------------------------------------------

class TestMCPToolTiers:
    """All registered MCP tools must have an explicit min_tier."""

    def test_all_tools_have_explicit_min_tier(self):
        """No tool should have been registered without an explicit min_tier."""
        # Import all tool modules to trigger registration
        import carrac.mcp.tools.alerts  # noqa
        import carrac.mcp.tools.analysis  # noqa
        import carrac.mcp.tools.backtest  # noqa
        import carrac.mcp.tools.bridges  # noqa
        import carrac.mcp.tools.context  # noqa
        import carrac.mcp.tools.guide  # noqa
        import carrac.mcp.tools.market  # noqa
        import carrac.mcp.tools.indicators  # noqa
        import carrac.mcp.tools.plays  # noqa
        import carrac.mcp.tools.quant  # noqa
        import carrac.mcp.tools.regime  # noqa
        import carrac.mcp.tools.reports  # noqa
        import carrac.mcp.tools.scanner  # noqa

        from carrac.mcp.registry import TOOL_REGISTRY, TIER_RANK

        missing = []
        for name, tool in TOOL_REGISTRY.items():
            tier = tool.get('min_tier')
            if tier is None or tier not in TIER_RANK:
                missing.append(f'{name!r}: {tier!r}')

        assert not missing, (
            f'Tools without valid explicit min_tier: {missing}. '
            'Add min_tier= to each @mcp_tool decorator.'
        )

    def test_paid_write_tools_not_free(self):
        """Writes that spin up recurring server-side compute stay paid.

        Free-beta policy: own-data CRUD (plays/drawings/trades/profile) is
        intentionally FREE — the user manages their own data at ~no marginal
        cost. Only writes that create recurring background work (alert
        evaluation, quant/regime alerts) remain gated above free.
        """
        import carrac.mcp.tools.alerts  # noqa
        import carrac.mcp.tools.bridges  # noqa
        import carrac.mcp.tools.quant  # noqa
        import carrac.mcp.tools.regime  # noqa

        from carrac.mcp.registry import TOOL_REGISTRY

        paid_write_tools = {
            'alert_manage', 'notification_configure', 'alert_create_from_drawing',
            'create_quant_alert', 'alert_create_regime_watch',
        }

        for tool_name in paid_write_tools:
            if tool_name in TOOL_REGISTRY:
                tier = TOOL_REGISTRY[tool_name]['min_tier']
                assert tier != 'free', (
                    f"Paid write tool {tool_name!r} should not be free"
                )

    def test_own_data_crud_is_free(self):
        """Own-data CRUD is free in the invite-only beta (MCP-first pivot)."""
        import carrac.mcp.tools.plays  # noqa
        import carrac.mcp.tools.journal  # noqa
        import carrac.mcp.tools.drawings  # noqa

        from carrac.mcp.registry import TOOL_REGISTRY

        free_crud = {'play_create', 'play_update', 'trade_log', 'drawing_create'}
        for tool_name in free_crud:
            if tool_name in TOOL_REGISTRY:
                assert TOOL_REGISTRY[tool_name]['min_tier'] == 'free', (
                    f"Own-data CRUD tool {tool_name!r} should be free in beta"
                )

    def test_heavy_compute_tools_gated(self):
        """Heavy compute tools should require at least member tier."""
        import carrac.mcp.tools.backtest  # noqa
        import carrac.mcp.tools.quant  # noqa

        from carrac.mcp.registry import TOOL_REGISTRY, TIER_RANK

        heavy_tools = {'backtest_play', 'scan_and_backtest', 'quant_get_analogues'}

        for tool_name in heavy_tools:
            if tool_name in TOOL_REGISTRY:
                tier = TOOL_REGISTRY[tool_name]['min_tier']
                assert TIER_RANK.get(tier, 0) >= TIER_RANK['member'], (
                    f"Heavy tool {tool_name!r} should require at least 'member', got {tier!r}"
                )


# ---------------------------------------------------------------------------
# Alerts blueprint has _enforce_alerts_csrf before_request
# ---------------------------------------------------------------------------

class TestAlertsBlueprintCSRF:
    """alerts_bp must have the _enforce_alerts_csrf hook registered."""

    def test_before_request_hook_present(self):
        """The alerts blueprint must have at least one before_request function."""
        from carrac.alerts.routes import alerts_bp
        # Flask blueprints store before_request funcs in .before_request_funcs
        # (keyed by None for blueprint-level hooks)
        hooks = alerts_bp.before_request_funcs.get(None, [])
        hook_names = [f.__name__ for f in hooks]
        assert '_enforce_alerts_csrf' in hook_names, (
            f'Expected _enforce_alerts_csrf in alerts_bp.before_request_funcs, got {hook_names}'
        )

    def test_no_per_route_csrf_exempt(self):
        """The alerts blueprint should not have per-route @csrf.exempt decorators."""
        import inspect
        import carrac.alerts.routes as routes_mod

        src = inspect.getsource(routes_mod)
        # The @csrf.exempt decorators should all be removed
        # (the remaining csrf usage is in the before_request hook itself)
        csrf_exempt_count = src.count('@csrf.exempt')
        assert csrf_exempt_count == 0, (
            f'Found {csrf_exempt_count} @csrf.exempt decorators in alerts routes — '
            'they should all be removed (replaced by _enforce_alerts_csrf hook)'
        )


# ---------------------------------------------------------------------------
# Assets blueprint has _enforce_assets_csrf before_request
# ---------------------------------------------------------------------------

class TestAssetsBlueprintCSRF:
    """assets_bp must have the _enforce_assets_csrf hook registered."""

    def test_before_request_hook_present(self):
        """The assets blueprint must have at least one before_request function."""
        from carrac.assets.routes import assets_bp
        hooks = assets_bp.before_request_funcs.get(None, [])
        hook_names = [f.__name__ for f in hooks]
        assert '_enforce_assets_csrf' in hook_names, (
            f'Expected _enforce_assets_csrf in assets_bp.before_request_funcs, got {hook_names}'
        )


# ---------------------------------------------------------------------------
# Plays routes use auth_or_key_required (not login_required)
# ---------------------------------------------------------------------------

class TestPlaysAuthDecorators:
    """Plays CRUD must use auth_or_key_required so API key callers get 401 JSON, not 302."""

    def test_no_login_required_on_crud_routes(self):
        """The plays routes module must not use @login_required on CRUD endpoints."""
        import inspect
        import carrac.plays.routes as routes_mod

        src = inspect.getsource(routes_mod)
        # @login_required should not appear at all — all replaced with @auth_or_key_required
        assert '@login_required' not in src, (
            'Found @login_required in plays routes — should all be @auth_or_key_required'
        )

    def test_auth_or_key_required_used(self):
        """auth_or_key_required must be imported and used."""
        import inspect
        import carrac.plays.routes as routes_mod

        src = inspect.getsource(routes_mod)
        assert '@auth_or_key_required' in src
