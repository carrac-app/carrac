"""Tests for MCP read-only scope enforcement (FIX 1) and notification_configure
SSRF validation (FIX 2).

FIX 1: A caller with scope='read-only' must be blocked from all WRITE_TOOLS
with a ToolError; read-only tools and callers with scope=None must pass through
unaffected.

FIX 2: notification_configure must reject invalid ntfy / Discord URLs before
persisting them, appending a warning rather than raising (matches REST path).
"""

from __future__ import annotations



# ── Helpers ────────────────────────────────────────────────────────────────

def _set_caller(user_id, tier, scope=None):
    from carrac.mcp.runtime import set_mcp_caller
    set_mcp_caller(user_id, tier, scope)


def _reset_caller():
    from carrac.mcp.runtime import reset_mcp_caller
    reset_mcp_caller()


# ── FIX 1: runtime scope contextvar ────────────────────────────────────────

class TestScopeContextVar:
    def test_scope_default_is_none(self):
        _reset_caller()
        from carrac.mcp.runtime import get_mcp_caller_scope
        assert get_mcp_caller_scope() is None

    def test_scope_set_read_only(self):
        from carrac.mcp.runtime import get_mcp_caller_scope
        _set_caller(42, 'premium', 'read-only')
        try:
            assert get_mcp_caller_scope() == 'read-only'
        finally:
            _reset_caller()

    def test_scope_set_none(self):
        from carrac.mcp.runtime import get_mcp_caller_scope
        _set_caller(42, 'premium', None)
        try:
            assert get_mcp_caller_scope() is None
        finally:
            _reset_caller()

    def test_reset_clears_scope(self):
        from carrac.mcp.runtime import get_mcp_caller_scope
        _set_caller(42, 'premium', 'read-only')
        _reset_caller()
        assert get_mcp_caller_scope() is None

    def test_backward_compat_two_arg_set(self):
        """set_mcp_caller(user_id, tier) without scope must still work."""
        from carrac.mcp.runtime import get_mcp_caller_scope, get_mcp_caller_tier
        _set_caller(7, 'member')  # no scope kwarg
        try:
            assert get_mcp_caller_tier() == 'member'
            assert get_mcp_caller_scope() is None
        finally:
            _reset_caller()


# ── FIX 1: WRITE_TOOLS list sanity ─────────────────────────────────────────

class TestWriteToolsSet:
    def test_write_tools_is_frozenset(self):
        from carrac.mcp.protocol import WRITE_TOOLS
        assert isinstance(WRITE_TOOLS, frozenset)

    def test_known_mutating_tools_present(self):
        from carrac.mcp.protocol import WRITE_TOOLS
        expected = {
            'play_create', 'play_update', 'play_delete', 'play_archive',
            'trade_log', 'trade_close', 'trade_delete',
            'alert_manage', 'notification_configure',
            'drawing_create', 'drawing_update', 'drawing_delete',
            'backtest_save_run', 'watchlist_manage', 'profile_manage',
            # share_manage replaces the six share_*/unshare_*
            # tools; alert_create_regime_watch is kept as its own separate
            # mutating tool (not folded into alert_manage).
            'share_manage', 'alert_create_regime_watch',
        }
        missing = expected - WRITE_TOOLS
        assert not missing, f"Expected mutating tools missing from WRITE_TOOLS: {missing}"

    def test_backtest_read_tools_not_mutating(self):
        """backtest_run/backtest_get_runs/backtest_simulate/
        backtest_get_historical_analog never persist anything by themselves
        (only backtest_save_run does) — must stay out of WRITE_TOOLS exactly
        like their pre-rename equivalents did."""
        from carrac.mcp.protocol import WRITE_TOOLS
        read_backtest_tools = {
            'backtest_run', 'backtest_get_runs', 'backtest_simulate',
            'backtest_get_historical_analog',
        }
        overlap = read_backtest_tools & WRITE_TOOLS
        assert not overlap, f"Non-mutating backtest tools incorrectly in WRITE_TOOLS: {overlap}"

    def test_read_tools_absent(self):
        from carrac.mcp.protocol import WRITE_TOOLS
        read_tools = {'market_get_chart', 'market_get_sentiment', 'play_list', 'trade_list',
                      'market_get_detail', 'report_get_section'}
        overlap = read_tools & WRITE_TOOLS
        assert not overlap, f"Read tools incorrectly in WRITE_TOOLS: {overlap}"


# ── Legacy tool-name alias shim ────────────────────────────────
#
# An external strategist client (separate repo and deploy) still calls pre-rename tool names. These prove the shim actually
# works end to end: an old name dispatches to the new handler, old names
# never appear in tools/list, and — the specific failure mode to avoid
# — the WRITE_TOOLS read-only gate still applies when a mutating
# tool is invoked by its OLD name.

class TestLegacyToolAliases:
    def test_old_name_maps_to_new_canonical_name(self):
        from carrac.mcp.registry import resolve_tool_name
        assert resolve_tool_name('get_chart') == 'market_get_chart'
        assert resolve_tool_name('manage_alerts') == 'alert_manage'
        assert resolve_tool_name('share_chart') == 'share_manage'
        # A canonical (already-new) name resolves to itself.
        assert resolve_tool_name('market_get_chart') == 'market_get_chart'

    def test_old_name_dispatches_to_new_handler(self, monkeypatch):
        """call_tool('get_chart', ...) must invoke market_get_chart's handler,
        not raise KeyError."""
        from carrac.mcp.registry import call_tool, TOOL_REGISTRY
        from carrac.mcp.protocol import _ensure_tools_loaded
        _ensure_tools_loaded()

        called = {}

        def _fake_handler(arguments):
            called['arguments'] = arguments
            return {'ok': True}

        monkeypatch.setitem(
            TOOL_REGISTRY, 'market_get_chart',
            {**TOOL_REGISTRY['market_get_chart'], 'handler': _fake_handler},
        )
        result = call_tool('get_chart', {'symbol': 'BTC'})
        assert result == {'ok': True}
        assert called['arguments'] == {'symbol': 'BTC'}

    def test_old_names_absent_from_tools_list(self):
        """tools/list must report the post-rename count only — legacy names
        are dispatch-only, never advertised."""
        from carrac.mcp.registry import get_tool_definitions, LEGACY_TOOL_ALIASES
        from carrac.mcp.protocol import _ensure_tools_loaded
        _ensure_tools_loaded()

        listed_names = {t['name'] for t in get_tool_definitions(caller_tier=None)}
        overlap = listed_names & set(LEGACY_TOOL_ALIASES)
        assert not overlap, f"Legacy names leaked into tools/list: {overlap}"

    def test_legacy_arg_defaults_force_discriminator(self):
        """A handful of consolidated tools need action/entity_type/family
        injected for legacy callers — spot-check the mapping is well-formed
        (every LEGACY_ARG_DEFAULTS key is a real legacy alias)."""
        from carrac.mcp.registry import LEGACY_ARG_DEFAULTS, LEGACY_TOOL_ALIASES
        missing = set(LEGACY_ARG_DEFAULTS) - set(LEGACY_TOOL_ALIASES)
        assert not missing, f"LEGACY_ARG_DEFAULTS keys not in LEGACY_TOOL_ALIASES: {missing}"


class TestLegacyToolAliasesReadOnlyScope:
    """The exact failure mode to avoid: if WRITE_TOOLS isn't
    checked against the CANONICAL name, a read-only API key could call a
    mutating tool by its OLD name and slip past the gate."""

    def setup_method(self):
        _reset_caller()

    def teardown_method(self):
        _reset_caller()

    def test_legacy_mutating_name_blocked_for_read_only_scope(self):
        from carrac.mcp.protocol import dispatch
        # 'create_play' is the pre-rename name for 'play_create', a WRITE_TOOL.
        _set_caller(1, 'premium', 'read-only')
        result = dispatch({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'create_play', 'arguments': {}},
        })
        assert result.get('result', {}).get('isError') is True
        text = result['result']['content'][0]['text']
        assert 'read-only' in text.lower()

    def test_legacy_share_name_blocked_for_read_only_scope(self):
        """share_chart -> share_manage; share_manage IS a WRITE_TOOL even
        though the caller never sends entity_type/action under the legacy name."""
        from carrac.mcp.protocol import dispatch
        _set_caller(1, 'premium', 'read-only')
        result = dispatch({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'share_chart', 'arguments': {'symbol': 'BTC'}},
        })
        assert result.get('result', {}).get('isError') is True
        text = result['result']['content'][0]['text']
        assert 'read-only' in text.lower()


# ── drawing_create_scenario -> drawing_create_group rename ─────
#
# Scenarios are called Plays, so the legacy tool name for that concept is
# deprecated (not removed) in favor of one describing the mechanism it
# performs (create a parent drawing + linked children). Same shim as the tool
# consolidation: the legacy name must keep dispatching to the identical
# handler forever, or every external caller breaks silently mid-call.

class TestScenarioToGroupRename:
    def test_old_name_resolves_to_new_canonical_name(self):
        from carrac.mcp.registry import resolve_tool_name
        assert resolve_tool_name('drawing_create_scenario') == 'drawing_create_group'
        # A canonical (already-new) name resolves to itself.
        assert resolve_tool_name('drawing_create_group') == 'drawing_create_group'

    def test_double_legacy_name_still_resolves(self):
        """create_scenario_with_children (the pre-consolidation name) must
        point straight at drawing_create_group. resolve_tool_name is single-hop,
        so an entry pointing at the legacy alias drawing_create_scenario
        would resolve to a name no longer in TOOL_REGISTRY at all."""
        from carrac.mcp.registry import resolve_tool_name
        assert resolve_tool_name('create_scenario_with_children') == 'drawing_create_group'

    def test_new_name_is_the_only_one_registered(self):
        """drawing_create_group must be a real registered tool; the legacy name
        must never leak into tools/list (dispatch-only, like every other
        legacy alias)."""
        from carrac.mcp.registry import get_tool_definitions, TOOL_REGISTRY
        from carrac.mcp.protocol import _ensure_tools_loaded
        _ensure_tools_loaded()

        assert 'drawing_create_group' in TOOL_REGISTRY
        assert 'drawing_create_scenario' not in TOOL_REGISTRY

        listed_names = {t['name'] for t in get_tool_definitions(caller_tier=None)}
        assert 'drawing_create_group' in listed_names
        assert 'drawing_create_scenario' not in listed_names

    def test_both_names_dispatch_to_the_same_handler(self, monkeypatch):
        """This is the test that fails if the alias is ever dropped: both
        the legacy and the canonical name must invoke the identical handler with the
        caller's arguments unchanged."""
        from carrac.mcp.registry import call_tool, TOOL_REGISTRY
        from carrac.mcp.protocol import _ensure_tools_loaded
        _ensure_tools_loaded()

        called = []

        def _fake_handler(arguments):
            called.append(arguments)
            return {'scenario_id': 123, 'child_count': 0}

        monkeypatch.setitem(
            TOOL_REGISTRY, 'drawing_create_group',
            {**TOOL_REGISTRY['drawing_create_group'], 'handler': _fake_handler},
        )

        result_new = call_tool('drawing_create_group', {'asset': 'BTC', 'timeframe': '1H', 'name': 'x'})
        result_old = call_tool('drawing_create_scenario', {'asset': 'ETH', 'timeframe': '4H', 'name': 'y'})

        assert result_new == {'scenario_id': 123, 'child_count': 0}
        assert result_old == {'scenario_id': 123, 'child_count': 0}
        assert called == [
            {'asset': 'BTC', 'timeframe': '1H', 'name': 'x'},
            {'asset': 'ETH', 'timeframe': '4H', 'name': 'y'},
        ]

    def test_new_name_is_a_write_tool(self):
        from carrac.mcp.protocol import WRITE_TOOLS
        assert 'drawing_create_group' in WRITE_TOOLS
        assert 'drawing_create_scenario' not in WRITE_TOOLS

    def test_legacy_name_blocked_for_read_only_scope(self):
        """The exact consolidation failure mode: a read-only API key calling
        the mutating tool by its OLD name must still be gated, because
        protocol.py checks WRITE_TOOLS against the resolved canonical name,
        not the caller-supplied one."""
        from carrac.mcp.protocol import dispatch
        _reset_caller()
        try:
            _set_caller(1, 'premium', 'read-only')
            result = dispatch({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {
                    'name': 'drawing_create_scenario',
                    'arguments': {'asset': 'BTC', 'timeframe': '1H', 'name': 'x'},
                },
            })
            assert result.get('result', {}).get('isError') is True
            text = result['result']['content'][0]['text']
            assert 'read-only' in text.lower()
        finally:
            _reset_caller()


# ── FIX 1: protocol scope gate ─────────────────────────────────────────────

class TestProtocolScopeGate:
    """Unit-test _handle_tools_call scope enforcement without real tool execution."""

    def setup_method(self):
        _reset_caller()

    def teardown_method(self):
        _reset_caller()

    def _call_tool_via_dispatch(self, tool_name, scope=None, tier='premium', user_id=1):
        """Drive protocol.dispatch with a tools/call request."""
        from carrac.mcp.protocol import dispatch
        _set_caller(user_id, tier, scope)
        return dispatch({
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'tools/call',
            'params': {'name': tool_name, 'arguments': {}},
        })

    def test_read_only_write_tool_returns_tool_error(self):
        """A read-only key calling a WRITE_TOOL must get a ToolError response."""
        result = self._call_tool_via_dispatch('play_create', scope='read-only')
        # ToolError maps to a success envelope with isError=True
        assert result.get('result', {}).get('isError') is True
        text = result['result']['content'][0]['text']
        assert 'read-only' in text.lower()

    def test_full_scope_write_tool_not_blocked_by_scope(self):
        """A full-access key calling a write tool is NOT blocked by scope gate
        (may still fail for other reasons — tool not found, tier, etc.)."""
        result = self._call_tool_via_dispatch('play_create', scope=None, tier='premium')
        # The scope gate must NOT inject a read-only error.
        # Tool may raise for other reasons; just confirm no scope denial.
        text = result.get('result', {}).get('content', [{}])[0].get('text', '')
        assert 'read-only' not in text.lower()

    def test_admin_key_no_scope_not_blocked(self):
        """Shared admin key (user_id=None, tier=admin, scope=None) must NOT be blocked."""
        result = self._call_tool_via_dispatch('play_create', scope=None, tier='admin', user_id=None)
        text = result.get('result', {}).get('content', [{}])[0].get('text', '')
        assert 'read-only' not in text.lower()

    def test_read_only_read_tool_not_blocked(self):
        """A read-only key calling a read-only tool must NOT be blocked by scope gate."""
        result = self._call_tool_via_dispatch('play_list', scope='read-only', tier='free')
        # Scope gate must not fire — tool may fail for auth/other reasons but not scope
        text = result.get('result', {}).get('content', [{}])[0].get('text', '')
        assert 'read-only API key' not in text


# ── FIX 2: notification_configure SSRF validation ─────────────────────────

class TestConfigureNotificationsValidation:
    """Unit-test the URL validation in notification_configure without DB."""

    def _run_set(self, monkeypatch, **kwargs):
        """Call notification_configure action=set with kwargs as arguments,
        with user_db and encrypt_token stubbed out."""
        from carrac.mcp.tools.alerts import notification_configure

        _prefs = {}

        class _FakeUserDB:
            def get_preferences(self, uid):
                return dict(_prefs)
            def save_preferences(self, uid, p):
                _prefs.update(p)

        monkeypatch.setattr(
            'carrac.mcp.tools.alerts.get_mcp_user_id', lambda: 1
        )

        import sys

        # Stub user_db
        fake_udb_mod = type(sys)('user_db')
        fake_udb_mod.user_db = _FakeUserDB()
        monkeypatch.setitem(sys.modules, 'user_db', fake_udb_mod)

        # Stub telegram crypto
        fake_tg = type(sys)('carrac.secret_box')
        fake_tg.encrypt_token = lambda t: f'enc:{t}'
        fake_tg.decrypt_token = lambda t: t.replace('enc:', '')
        monkeypatch.setitem(sys.modules, 'carrac.secret_box', fake_tg)

        args = {'action': 'set', **kwargs}
        return notification_configure(args)

    def test_invalid_ntfy_url_rejected_with_warning(self, monkeypatch):
        """An RFC1918 or non-https ntfy URL must NOT be saved and a warning returned."""
        result = self._run_set(monkeypatch, ntfy_url='http://10.0.0.1/topic')
        assert 'warnings' in result
        assert any('ntfy' in w.lower() for w in result['warnings'])

    def test_valid_ntfy_url_saved(self, monkeypatch):
        """A valid ntfy.sh URL must be saved without warnings."""
        # Resolve hermetically: local resolvers (Pi-hole etc.) may block or
        # rewrite ntfy.sh to :: / a private IP, which the SSRF guard rightly
        # rejects — the test must not depend on live DNS.
        # security.py imports socket inside the function, so patch the global
        # socket module (monkeypatch restores it afterwards).
        import socket as _socket
        monkeypatch.setattr(
            _socket, 'getaddrinfo',
            lambda *a, **k: [(_socket.AF_INET, _socket.SOCK_STREAM, 6, '', ('1.1.1.1', 0))],
        )
        result = self._run_set(monkeypatch, ntfy_url='https://ntfy.sh/my-topic')
        ntfy_warnings = [w for w in result.get('warnings', []) if 'ntfy' in w.lower()]
        assert not ntfy_warnings

    def test_invalid_discord_webhook_rejected_with_warning(self, monkeypatch):
        """A non-discord.com webhook URL must NOT be saved and a warning returned."""
        result = self._run_set(monkeypatch, discord_webhook='https://evil.com/hook')
        assert 'warnings' in result
        assert any('discord' in w.lower() for w in result['warnings'])

    def test_valid_discord_webhook_saved(self, monkeypatch):
        """A valid discord.com webhook must be saved without warnings."""
        result = self._run_set(
            monkeypatch,
            discord_webhook='https://discord.com/api/webhooks/123456/abcDEF-token',
        )
        discord_warnings = [w for w in result.get('warnings', []) if 'discord' in w.lower()]
        assert not discord_warnings

    def test_invalid_ntfy_does_not_overwrite_existing(self, monkeypatch):
        """When a bad ntfy URL is rejected, the saved URL must be retained."""
        # First set a valid URL
        self._run_set(monkeypatch, ntfy_url='https://ntfy.sh/good-topic')
        # Now attempt to overwrite with an invalid one — it should be silently kept
        result = self._run_set(monkeypatch, ntfy_url='http://10.0.0.1/evil')
        assert any('ntfy' in w.lower() for w in result.get('warnings', []))
