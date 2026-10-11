"""
Tests for the admin health-check framework.

Coverage:
  - run_all_checks() returns all registered components.
  - run_all_checks() NEVER raises even when every dependency is down.
  - Individual checks return RED (not exceptions) when mocked to fail.
  - The /admin/health route requires admin auth (redirect/401 without).
  - The /api/admin/health JSON endpoint requires admin auth.
  - The indicator check classifies a good vs broken indicator correctly.
  - The cache layer works (~30s TTL) and force=True bypasses it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find(results, component):
    for r in results:
        if r['component'] == component:
            return r
    return None


# =============================================================================
# 1. Framework: run_all_checks completeness & resilience
# =============================================================================

class TestRunAllChecks:
    """run_all_checks() contract — completeness and never-raise."""

    def test_returns_list_of_dicts(self, app):
        with app.app_context():
            from carrac.admin.health_checks import run_all_checks
            results = run_all_checks(force=True)
            assert isinstance(results, list)
            assert len(results) > 0

    def test_all_results_have_required_keys(self, app):
        with app.app_context():
            from carrac.admin.health_checks import run_all_checks
            results = run_all_checks(force=True)
            required = {'component', 'category', 'status', 'detail', 'checked_at', 'latency_ms'}
            for r in results:
                assert required.issubset(r.keys()), f"Missing keys in: {r}"

    def test_all_results_have_valid_status(self, app):
        with app.app_context():
            from carrac.admin.health_checks import run_all_checks, GREEN, AMBER, RED
            results = run_all_checks(force=True)
            valid = {GREEN, AMBER, RED}
            for r in results:
                assert r['status'] in valid, f"Invalid status in: {r}"

    def test_expected_components_present(self, app):
        """The 8 built-in checks must all appear in results."""
        with app.app_context():
            from carrac.admin.health_checks import run_all_checks
            results = run_all_checks(force=True)
            components = {r['component'] for r in results}
            expected = {
                'database', 'redis', 'worker', 'indicators',
                'data_sources', 'morning_engine', 'ai', 'billing', 'mcp',
            }
            missing = expected - components
            assert not missing, f"Components missing from results: {missing}"

    def test_does_not_raise_when_all_deps_down(self, app, monkeypatch):
        """Every check must catch its own exceptions — run_all_checks must not raise."""
        with app.app_context():
            # Patch every plausible import to explode
            monkeypatch.setattr(
                'carrac.admin.health_checks.check_database',
                lambda: (_ for _ in ()).throw(RuntimeError("DB gone")),
            )

            from carrac.admin import health_checks as hc
            # Directly call hc.run_all_checks with a deliberately broken check injected.
            # We do this by temporarily replacing _REGISTRY entries.
            original = list(hc._REGISTRY)
            hc._REGISTRY = [
                {
                    'component': 'exploding',
                    'category': 'test',
                    'fn': lambda: 1 / 0,         # ZeroDivisionError
                },
                {
                    'component': 'normal',
                    'category': 'test',
                    'fn': lambda: {'status': hc.GREEN, 'detail': 'ok'},
                },
            ]
            try:
                results = hc.run_all_checks(force=True)
                # Must not raise; the exploding check must return RED
                boom = _find(results, 'exploding')
                assert boom is not None
                assert boom['status'] == hc.RED
                # The normal check must still be GREEN
                good = _find(results, 'normal')
                assert good is not None
                assert good['status'] == hc.GREEN
            finally:
                hc._REGISTRY = original

    def test_latency_ms_is_non_negative_int(self, app):
        with app.app_context():
            from carrac.admin.health_checks import run_all_checks
            results = run_all_checks(force=True)
            for r in results:
                assert isinstance(r['latency_ms'], int)
                assert r['latency_ms'] >= 0

    def test_force_bypasses_cache(self, app):
        """Two consecutive force=True calls must run the checks twice."""
        with app.app_context():
            from carrac.admin import health_checks as hc
            original = list(hc._REGISTRY)
            call_log = []
            hc._REGISTRY = [{
                'component': 'counter',
                'category': 'test',
                'fn': lambda: call_log.append(1) or {'status': hc.GREEN, 'detail': 'ok'},
            }]
            try:
                hc.run_all_checks(force=True)
                hc.run_all_checks(force=True)
                assert len(call_log) == 2
            finally:
                hc._REGISTRY = original

    def test_cache_avoids_rerun_within_ttl(self, app):
        """A second call within TTL must NOT rerun checks."""
        with app.app_context():
            from carrac.admin import health_checks as hc
            original = list(hc._REGISTRY)
            call_log = []
            hc._REGISTRY = [{
                'component': 'counter',
                'category': 'test',
                'fn': lambda: call_log.append(1) or {'status': hc.GREEN, 'detail': 'ok'},
            }]
            try:
                hc.run_all_checks(force=True)   # populates cache
                hc.run_all_checks()              # should hit cache
                assert len(call_log) == 1, "Check ran twice despite cache"
            finally:
                hc._REGISTRY = original


# =============================================================================
# 2. Individual check: RED on failure
# =============================================================================

class TestCheckRedOnFailure:
    """Each check must return RED (not raise) when its dependency is broken."""

    def test_database_check_red_on_exception(self, app, monkeypatch):
        with app.app_context():
            import carrac.admin.health_checks as hc
            monkeypatch.setattr('carrac.db_pool.get_conn', MagicMock(side_effect=Exception("DB gone")))
            result = hc.check_database()
            assert result['status'] == hc.RED

    def test_redis_check_red_when_unavailable(self, app, monkeypatch):
        with app.app_context():
            import carrac.admin.health_checks as hc
            monkeypatch.setattr('carrac.redis_cache.get_redis', lambda: None)
            result = hc.check_redis()
            assert result['status'] == hc.RED

    def test_worker_check_red_when_key_absent(self, app, monkeypatch):
        with app.app_context():
            import carrac.admin.health_checks as hc
            mock_r = MagicMock()
            mock_r.get.return_value = None
            monkeypatch.setattr('carrac.caches._get_redis', lambda: mock_r)
            result = hc.check_worker()
            assert result['status'] == hc.RED

    def test_billing_check_amber_when_payments_disabled(self, app, monkeypatch):
        # Both keys absent = payments intentionally off (beta) → AMBER, so the
        # overall health endpoint stays 200 for Kuma/Watchdog instead of 503.
        with app.app_context():
            import carrac.admin.health_checks as hc
            import carrac.config as cfg
            monkeypatch.setattr(cfg.Config, 'PADDLE_WEBHOOK_SECRET', '')
            monkeypatch.setattr(cfg.Config, 'PADDLE_API_KEY', '')
            result = hc.check_billing()
            assert result['status'] == hc.AMBER
            assert 'payments disabled' in result['detail']

    def test_billing_check_red_when_keys_partial(self, app, monkeypatch):
        # Exactly one key set = a real misconfiguration → RED.
        with app.app_context():
            import carrac.admin.health_checks as hc
            import carrac.config as cfg
            monkeypatch.setattr(cfg.Config, 'PADDLE_WEBHOOK_SECRET', '')
            monkeypatch.setattr(cfg.Config, 'PADDLE_API_KEY', 'pdl_live_xyz')
            result = hc.check_billing()
            assert result['status'] == hc.RED
            assert 'PADDLE_WEBHOOK_SECRET' in result['detail']

    def test_ai_check_red_when_grounding_disabled(self, app, monkeypatch):
        with app.app_context():
            import carrac.admin.health_checks as hc
            monkeypatch.setenv('GROUNDING_ENABLED', '0')
            # Redis unavailable for spend — default to 0 spend
            monkeypatch.setattr(
                'carrac.ai.billing.get_global_ai_spend_cents_today', lambda: 0
            )
            result = hc.check_ai()
            assert result['status'] == hc.RED
            assert 'GROUNDING_ENABLED' in result['detail']

    @staticmethod
    def _morning_report(monkeypatch, report):
        monkeypatch.delenv('ENABLE_MORNING_REPORT', raising=False)
        db = MagicMock()
        db.get_latest_report.return_value = report
        monkeypatch.setattr('carrac.content_db.get_content_db', lambda: db)

    def test_morning_engine_amber_when_no_reports(self, app, monkeypatch):
        with app.app_context():
            import carrac.admin.health_checks as hc
            self._morning_report(monkeypatch, None)
            assert hc.check_morning_engine()['status'] == hc.AMBER

    def test_morning_engine_green_for_fresh_and_red_for_stalled(self, app, monkeypatch):
        from datetime import datetime, timedelta
        with app.app_context():
            import carrac.admin.health_checks as hc
            now = datetime.utcnow()
            self._morning_report(monkeypatch, {'id': 1, 'created_at': (now - timedelta(hours=2)).isoformat()})
            assert hc.check_morning_engine()['status'] == hc.GREEN
            self._morning_report(monkeypatch, {'id': 2, 'created_at': (now - timedelta(hours=200)).isoformat()})
            assert hc.check_morning_engine()['status'] == hc.RED

    def test_mcp_check_red_on_import_error(self, app, monkeypatch):
        import sys
        with app.app_context():
            import carrac.admin.health_checks as hc
            import carrac.mcp.tools  # noqa: F401 — make sure it is loaded, then hide it
            # A None entry in sys.modules makes `import carrac.mcp.tools` raise ImportError.
            monkeypatch.setitem(sys.modules, 'carrac.mcp.tools', None)
            result = hc.check_mcp()
            assert result['status'] == hc.RED
            assert 'MCP check error' in result['detail']

    def test_mcp_check_amber_on_empty_registry_green_otherwise(self, app, monkeypatch):
        with app.app_context():
            import carrac.admin.health_checks as hc
            with patch('carrac.mcp.registry.TOOL_REGISTRY', {}):
                assert hc.check_mcp()['status'] == hc.AMBER
            with patch('carrac.mcp.registry.TOOL_REGISTRY', {'t': object()}):
                assert hc.check_mcp()['status'] == hc.GREEN


# =============================================================================
# 3. Indicator check: good vs broken
# =============================================================================

class TestIndicatorCheck:
    """check_indicators() must correctly classify indicator health."""

    def test_good_indicator_produces_result_with_detail(self, app):
        """With a well-formed asset in cache, indicator check should run and return detail.

        NOTE: vocab drift between indicator_state.py and conditions.py may be surfaced
        as amber/red — that is the point of the check.  We verify it returns a result
        dict with a non-empty detail rather than crashing.
        """
        with app.app_context():
            from carrac.admin.health_checks import check_indicators, GREEN, AMBER, RED

            # Provide a rich asset dict.  Some indicators deliberately match the
            # in-vocabulary states fully (ema_ribbon, bb, consensio, macd) so
            # those contribute green_count even if others show drift.
            good_asset = {
                'symbol': 'BTC',
                'type': 'crypto',
                'latest_price': 50000.0,
                'realtime': {'price': 50100.0},
                'ema_ribbon': {'trend': 'bullish', 'days_since_flip': 5},
                'weekly_rsi': 62.0,
                'bb': {'upper': 52000, 'lower': 48000, 'width_pct': 8.0,
                       'squeeze': False, 'position': 'inside'},
                'consensio': {'total': {'signal': 2, 'positions': 5, 'max_positions': 9}},
                'structure': {'structure_trend': 'bullish'},
                'macd': {'histogram': 150.0},
                'adx': {'adx': 30.0, 'regime': 'strong_trend'},
                'cpr': {'price_level': 'CP-TC'},
                'cockpit_extended': {
                    'supertrend': {'direction': 'long'},
                    'psar': {'direction': 'long'},
                },
            }
            mock_cache_data = {'assets': [good_asset]}

            with patch('carrac.redis_cache.ResponseCache.get', return_value=mock_cache_data):
                result = check_indicators()
                # The check must return a valid result dict — it must not raise.
                assert result['status'] in (GREEN, AMBER, RED)
                assert isinstance(result['detail'], str)
                assert len(result['detail']) > 0
                # With a non-trivial asset the detail must mention "cache canary"
                assert 'cache canary' in result['detail'], (
                    f"Expected 'cache canary' in detail: {result['detail']}"
                )

    def test_broken_indicator_emits_detail(self, app):
        """With a blank asset dict, null counts should be high but no crash."""
        with app.app_context():
            from carrac.admin.health_checks import check_indicators

            empty_asset = {'symbol': 'TEST', 'type': 'crypto'}
            mock_cache_data = {'assets': [empty_asset]}

            with patch('carrac.redis_cache.ResponseCache.get', return_value=mock_cache_data):
                result = check_indicators()
                # Should not raise; should have a detail string
                assert isinstance(result['detail'], str)
                assert len(result['detail']) > 0

    # The 12 states below were wired into indicator_state.py's
    # TRACKED_STATES (Alerts evaluator) but had no branch in conditions.py's
    # _get_indicator_state (Plays evaluator) — the check_indicators() canary
    # loop (above) flagged each as "one-sided" (amber) because
    # fp.get(key) (alerts side) was non-None while _cond_get_state(key.upper(),
    # ...) (plays side) always returned None. Now wired — verify the gap is gone.
    NEWLY_WIRED_KEYS = [
        'rsi_zone', 'adx_regime', 'cpr_position', 'weekly_cpr_position',
        'daily_cpr_position', 'ema_gap_squeeze', 'v_cpr', 'cpr_extreme',
        'structure_disagreement', 'tf_divergence', 'rsi_divergence', 'perp_basis',
    ]

    def _newly_wired_canary_asset(self):
        """A canary asset with real data for every TRACKED_STATES indicator
        (not just the 12 newly wired states) so the check_indicators() canary
        loop has no *other* pre-existing one-sided gaps to confound the
        assertion below."""
        return {
            'symbol': 'BTC',
            'type': 'crypto',
            'latest_price': 50000.0,
            'realtime': {'price': 50100.0},
            'ema_ribbon': {'trend': 'bullish', 'days_since_flip': 5},
            'weekly_rsi': 62.0,
            'bb': {'upper': 52000, 'lower': 48000, 'width_pct': 8.0,
                   'squeeze': False, 'position': 'inside'},
            'consensio': {'total': {'signal': 2, 'positions': 5, 'max_positions': 9}},
            'structure': {'structure_trend': 'bullish'},
            'macd': {'histogram': 150.0},
            'adx': {'adx': 30.0, 'regime': 'strong_trend'},
            'cpr': {'price_level': 'CP-TC', 'price_position': 'above'},
            'weekly_cpr': {'price_level': 'BC-CP', 'price_position': 'below'},
            'daily_cpr': {'price_level': 'TC-R1', 'price_position': 'above'},
            'cockpit_extended': {
                'supertrend': {'direction': 'long'},
                'psar': {'direction': 'long'},
                'ichimoku': {'cloud_position': 'above_green', 'tk_cross': 'bullish'},
                'ttm_squeeze': {'active': False, 'momentum': 'bullish'},
                'alligator': {'state': 'eating_bullish'},
                'ema_cross': {'state': 'bullish'},
                'donchian': {'upper': 52000.0, 'lower': 48000.0},
                'stoch_rsi': {'zone': 'neutral'},
            },
            'score_b': {'alerts': ['ema_squeeze', 'v_cpr', 'near_r3', 'structure_bullish']},
            'trend_analysis': {'patterns': ['divergence', 'rsi_bull_div']},
            'perp_basis_pct': 0.5,
        }

    def test_newly_wired_states_no_longer_one_sided(self, app):
        """Direct-mechanism check: for each of the 12 newly wired keys, when
        the alerts-side extractor (extract_fingerprint) returns a non-None
        state, the plays-side extractor (_get_indicator_state) must also
        return non-None — exactly the condition check_indicators() uses to
        populate amber_names."""
        with app.app_context():
            from carrac.indicator_state import extract_fingerprint
            from carrac.conditions import _get_indicator_state as _cond_get_state

            canary_asset = self._newly_wired_canary_asset()
            fp = extract_fingerprint(canary_asset)

            for key in self.NEWLY_WIRED_KEYS:
                alerts_state = fp.get(key)
                plays_state = _cond_get_state(key.upper(), canary_asset)
                assert alerts_state is not None, f"{key}: alerts-side unexpectedly None"
                assert plays_state is not None, (
                    f"{key}: plays-side is None while alerts-side is {alerts_state!r} "
                    f"— one-sided (amber) wiring gap"
                )

    def test_newly_wired_states_not_amber_in_check_indicators(self, app):
        """Integration check: run the real check_indicators() against a
        mocked cache holding the fully-populated canary asset, and confirm
        none of the 12 newly wired keys appear in amber_names (the
        'one-sided: ...' segment of the detail string)."""
        with app.app_context():
            from carrac.admin.health_checks import check_indicators

            canary_asset = self._newly_wired_canary_asset()
            mock_cache_data = {'assets': [canary_asset]}

            with patch('carrac.redis_cache.ResponseCache.get', return_value=mock_cache_data):
                result = check_indicators()

            detail = result['detail']
            if 'one-sided:' in detail:
                one_sided_part = detail.split('one-sided:')[1].split(';')[0]
                for key in self.NEWLY_WIRED_KEYS:
                    assert key not in one_sided_part, (
                        f"{key} still flagged one-sided: {detail}"
                    )

    def test_wiring_gap_detected(self, app):
        """A real silent-failure signal must be flagged: an alerts-advertised
        indicator with NO tracked state (wiring gap) → the alert can never fire.

        Cross-system vocab differences between plays and alerts (e.g. STRUCTURE
        plays=HH_HL vs alerts=bullish) are INTENTIONAL and must NOT be flagged —
        that was a prior false positive."""
        with app.app_context():
            from carrac.admin import health_checks as hc
            from carrac.indicator_state import INDICATOR_ALERT_CONDITIONS

            original = dict(INDICATOR_ALERT_CONDITIONS)
            # Fake alerts indicator with no corresponding TRACKED_STATES entry.
            INDICATOR_ALERT_CONDITIONS['__fake_unwired__'] = ['up', 'down']
            try:
                with patch('carrac.redis_cache.ResponseCache.get', return_value=None):
                    result = hc.check_indicators()
                    assert result['status'] in (hc.AMBER, hc.RED)
                    assert ('drift' in result['detail'].lower()
                            or 'tracked' in result['detail'].lower()), result['detail']
            finally:
                INDICATOR_ALERT_CONDITIONS.clear()
                INDICATOR_ALERT_CONDITIONS.update(original)


# =============================================================================
# 4. Route auth: /admin/health requires admin session
# =============================================================================

# The admin_required decorator does a DB re-verification of admin status
# (user_db.get_user_by_id) which in SQLite test mode returns None unless
# we stub it.  We patch user_db at the point the decorator imports it.
_ADMIN_USER_STUB = {
    'id': 'admin',
    'email': 'admin@example.com',
    'is_admin': True,
    'is_active': True,
}


class TestAdminHealthRoute:
    """Admin route access-control tests."""

    def test_health_page_requires_admin(self, client):
        """Anonymous request must redirect or 401, not 200."""
        response = client.get('/admin/health')
        assert response.status_code in (302, 401), (
            f"Expected 302/401 for anonymous /admin/health, got {response.status_code}"
        )

    def test_health_page_requires_admin_not_user(self, auth_client):
        """Regular logged-in user (non-admin) must be denied."""
        response = auth_client.get('/admin/health')
        assert response.status_code in (302, 401, 403), (
            f"Expected redirect/deny for non-admin, got {response.status_code}"
        )

    def test_health_page_accessible_by_admin(self, admin_client, monkeypatch):
        """Admin session must get a 200 response."""
        # Stub the DB re-verification performed by admin_required
        import user_db as _udb
        monkeypatch.setattr(_udb.user_db, 'get_user_by_id', lambda uid: _ADMIN_USER_STUB)
        response = admin_client.get('/admin/health')
        assert response.status_code == 200, (
            f"Expected 200 for admin /admin/health, got {response.status_code}"
        )
        assert b'System Health' in response.data or b'health' in response.data.lower()

    def test_api_health_requires_admin(self, client):
        """JSON API endpoint must refuse anonymous access."""
        response = client.get('/api/admin/health',
                              headers={'Accept': 'application/json'})
        assert response.status_code in (302, 401)

    def test_api_health_returns_json_for_admin(self, admin_client, monkeypatch):
        """Admin must get a JSON payload with 'overall' and 'checks' keys."""
        import user_db as _udb
        monkeypatch.setattr(_udb.user_db, 'get_user_by_id', lambda uid: _ADMIN_USER_STUB)
        response = admin_client.get('/api/admin/health')
        assert response.status_code in (200, 503)
        data = response.get_json()
        assert data is not None, "Expected JSON response"
        assert 'overall' in data
        assert 'checks' in data
        assert isinstance(data['checks'], list)

    def test_api_health_overall_green_returns_200(self, admin_client, monkeypatch):
        """When overall is green/amber the JSON endpoint returns 200."""
        import user_db as _udb
        import carrac.admin.health_checks as hc
        monkeypatch.setattr(_udb.user_db, 'get_user_by_id', lambda uid: _ADMIN_USER_STUB)
        monkeypatch.setattr(hc, 'run_all_checks', lambda force=False: [
            {'component': 'test', 'category': 'test', 'status': hc.GREEN,
             'detail': 'ok', 'checked_at': '2026-01-01T00:00:00+00:00', 'latency_ms': 1},
        ])
        response = admin_client.get('/api/admin/health')
        assert response.status_code == 200
        assert response.get_json()['overall'] == hc.GREEN

    def test_api_health_overall_red_returns_503(self, admin_client, monkeypatch):
        """When any check is red the JSON endpoint returns 503."""
        import user_db as _udb
        import carrac.admin.health_checks as hc
        monkeypatch.setattr(_udb.user_db, 'get_user_by_id', lambda uid: _ADMIN_USER_STUB)
        monkeypatch.setattr(hc, 'run_all_checks', lambda force=False: [
            {'component': 'test', 'category': 'test', 'status': hc.RED,
             'detail': 'broken', 'checked_at': '2026-01-01T00:00:00+00:00', 'latency_ms': 1},
        ])
        response = admin_client.get('/api/admin/health')
        assert response.status_code == 503
        assert response.get_json()['overall'] == hc.RED
