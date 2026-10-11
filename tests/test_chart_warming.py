"""Proactive chart-cache warming (worker-side).

Coverage:
  * ``_build_chart_warm_set`` — bounded (symbol, tf) fan-out: core hot-tier
    symbols get every browsed HTF + 1h/4h, watchlist-only crypto gets 1D/4h,
    tracked non-crypto watchlist symbols get 1D/1W, capped at
    ``_CHART_WARM_MAX_KEYS`` with a logged (never silent) drop.
  * ``_chart_warm_lookup_tf`` — the cache-key suffix formula MUST match
    routes.py's ``_cache_suffix`` construction bit-for-bit (":col"
    always, intraday bucket == epoch_ms // interval_ms at the same instant),
    or a warmed key is never the one a real request looks up.
  * ``_warm_chart_cache`` orchestration — skip-if-fresh short-circuit, lock
    contention, per-cycle time budget, the CHART_WARM_ENABLED kill-switch,
    and the mode/query shape handed to the (mocked) rebuild.

``_run_chart_refresh`` (the actual chart rebuild) is ALWAYS mocked here —
this suite must never execute a real chart build or touch Redis/Postgres.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import carrac.precompute as precompute
from carrac.charts.routes import _FORMING_INTERVAL_MS


# ---------------------------------------------------------------------------
# _build_chart_warm_set — bounded fan-out
# ---------------------------------------------------------------------------

class TestBuildChartWarmSet:
    @pytest.fixture(autouse=True)
    def _no_non_crypto(self, monkeypatch):
        # The non-crypto tail reads user_db + the provider maps;
        # each test below sets it explicitly when it matters.
        monkeypatch.setattr(precompute, '_get_watchlist_non_crypto_symbols', lambda: [])

    def test_core_gets_every_core_tf(self, monkeypatch):
        monkeypatch.setattr(
            precompute, '_get_hot_tier_symbols',
            lambda: list(precompute._LTF_HOT_TIER_CORE),
        )
        pairs = set(precompute._build_chart_warm_set())
        for sym in precompute._LTF_HOT_TIER_CORE:
            for tf in precompute._CHART_WARM_CORE_TFS:
                assert (sym, tf) in pairs

    def test_core_tfs_cover_the_ledger_htfs(self):
        # 3D/1W/1M were the top non-BTC ledger pairs.
        assert {'1D', '3D', '1W', '1M', '4h'} <= set(precompute._CHART_WARM_CORE_TFS)
        # Sub-hourly stays out: ~5s rebuilds, and its ledger mass was the preload.
        assert not {'1m', '3m', '5m', '15m', '30m'} & set(precompute._CHART_WARM_CORE_TFS)

    def test_watchlist_only_symbols_get_watchlist_tfs(self, monkeypatch):
        extra = ['SHIB', 'PEPE']
        monkeypatch.setattr(
            precompute, '_get_hot_tier_symbols',
            lambda: list(precompute._LTF_HOT_TIER_CORE) + extra,
        )
        pairs = precompute._build_chart_warm_set()
        for sym in extra:
            tfs = {tf for s, tf in pairs if s == sym}
            assert tfs == set(precompute._CHART_WARM_WATCHLIST_TFS)

    def test_non_crypto_watchlist_gets_htf_only(self, monkeypatch):
        monkeypatch.setattr(
            precompute, '_get_hot_tier_symbols',
            lambda: list(precompute._LTF_HOT_TIER_CORE),
        )
        monkeypatch.setattr(precompute, '_get_watchlist_non_crypto_symbols', lambda: ['GOLD', 'SPX'])
        pairs = precompute._build_chart_warm_set()
        for sym in ('GOLD', 'SPX'):
            tfs = {tf for s, tf in pairs if s == sym}
            assert tfs == set(precompute._CHART_WARM_NONCRYPTO_TFS)
            # never 1h/4h: non-crypto intraday goes through Yahoo's shared budget
            assert not tfs & {'1h', '4h', '5m', '15m', '30m'}

    def test_cap_drops_the_non_crypto_tail_before_core(self, monkeypatch, caplog):
        # Order of priority when the cap bites: core, watchlist crypto, non-crypto.
        core = list(precompute._LTF_HOT_TIER_CORE)
        monkeypatch.setattr(precompute, '_get_hot_tier_symbols', lambda: core + ['SHIB'])
        many = [f'EQ{i}' for i in range(200)]
        monkeypatch.setattr(precompute, '_get_watchlist_non_crypto_symbols', lambda: many)
        with caplog.at_level('INFO', logger='carrac.precompute'):
            pairs = precompute._build_chart_warm_set()
        syms = {s for s, _ in pairs}
        assert set(core) <= syms and 'SHIB' in syms
        assert len(pairs) == precompute._CHART_WARM_MAX_KEYS
        assert any('capped' in r.getMessage() for r in caplog.records)

    def test_capped_at_max_keys_and_logs_drop(self, monkeypatch, caplog):
        # core (12) * 6 TFs = 72 keys; 80 extra watchlist symbols * 2 TFs = 160
        # more push well past the 200-key cap.
        extra = [f'SYM{i}' for i in range(80)]
        monkeypatch.setattr(
            precompute, '_get_hot_tier_symbols',
            lambda: list(precompute._LTF_HOT_TIER_CORE) + extra,
        )
        with caplog.at_level('INFO', logger='carrac.precompute'):
            pairs = precompute._build_chart_warm_set()
        assert len(pairs) <= precompute._CHART_WARM_MAX_KEYS
        assert any(
            'capped' in r.getMessage() and 'dropped' in r.getMessage()
            for r in caplog.records
        )

    def test_no_drop_log_when_under_cap(self, monkeypatch, caplog):
        monkeypatch.setattr(
            precompute, '_get_hot_tier_symbols',
            lambda: list(precompute._LTF_HOT_TIER_CORE),
        )
        with caplog.at_level('INFO', logger='carrac.precompute'):
            precompute._build_chart_warm_set()
        assert not any('capped' in r.getMessage() for r in caplog.records)

    def test_empty_hot_tier_yields_empty_set(self, monkeypatch):
        monkeypatch.setattr(precompute, '_get_hot_tier_symbols', lambda: [])
        assert precompute._build_chart_warm_set() == []


# ---------------------------------------------------------------------------
# _chart_warm_lookup_tf — must match routes.py's key formula
# ---------------------------------------------------------------------------

class TestChartWarmLookupTf:
    def test_htf_has_no_bucket_suffix(self):
        assert precompute._chart_warm_lookup_tf('1D') == '1D:col'

    def test_intraday_bucket_matches_routes_formula_at_fixed_time(self, monkeypatch):
        """Freeze time and independently recompute routes.py's OWN bucket
        formula (`int(_time.time() * 1000) // iv_ms`, carrac/charts/routes.py)
        — the two must agree bit-for-bit, or a warmed key silently misses the
        key a real request looks up."""
        fixed_ts = 1_752_000_123.456
        monkeypatch.setattr(precompute.time, 'time', lambda: fixed_ts)

        for tf in ('1h', '4h', '15m'):
            iv_ms = _FORMING_INTERVAL_MS[tf]
            routes_bucket = int(fixed_ts * 1000) // iv_ms
            assert precompute._chart_warm_lookup_tf(tf) == f'{tf}:col:b{routes_bucket}'

    def test_unknown_tf_falls_back_to_no_bucket(self):
        # '1W'/'3D'/'1M' aren't in _FORMING_INTERVAL_MS — same as HTF.
        assert precompute._chart_warm_lookup_tf('1W') == '1W:col'


# ---------------------------------------------------------------------------
# _warm_chart_cache — orchestration
# ---------------------------------------------------------------------------

@pytest.fixture
def _warm_env(monkeypatch):
    """Common plumbing: a live (non-None) fake Flask app so the pass doesn't
    bail out early, plus mocked collaborators so no real
    Redis/Flask/chart work ever happens in this suite."""
    monkeypatch.setattr(precompute, '_get_chart_warm_app', lambda: MagicMock(name='flask_app'))
    mock_lock = MagicMock(return_value=True)
    mock_refresh = MagicMock()
    monkeypatch.setattr('carrac.charts.routes._try_acquire_chart_refresh_lock', mock_lock)
    monkeypatch.setattr('carrac.charts.routes._run_chart_refresh', mock_refresh)
    return {'lock': mock_lock, 'refresh': mock_refresh}


class TestWarmChartCacheKillSwitch:
    @pytest.mark.parametrize('value', ['0', 'false', 'False', 'no', 'NO'])
    def test_disabled_values_skip_the_pass(self, monkeypatch, _warm_env, value):
        monkeypatch.setenv('CHART_WARM_ENABLED', value)
        build_set = MagicMock()
        monkeypatch.setattr(precompute, '_build_chart_warm_set', build_set)
        precompute._warm_chart_cache()
        build_set.assert_not_called()
        _warm_env['refresh'].assert_not_called()

    def test_default_is_enabled(self, monkeypatch, _warm_env):
        monkeypatch.delenv('CHART_WARM_ENABLED', raising=False)
        build_set = MagicMock(return_value=[])
        monkeypatch.setattr(precompute, '_build_chart_warm_set', build_set)
        precompute._warm_chart_cache()
        build_set.assert_called_once()

    def test_explicit_1_is_enabled(self, monkeypatch, _warm_env):
        monkeypatch.setenv('CHART_WARM_ENABLED', '1')
        build_set = MagicMock(return_value=[])
        monkeypatch.setattr(precompute, '_build_chart_warm_set', build_set)
        precompute._warm_chart_cache()
        build_set.assert_called_once()


class TestWarmChartCacheNoApp:
    def test_no_flask_app_available_skips_before_building_set(self, monkeypatch):
        monkeypatch.setattr(precompute, '_get_chart_warm_app', lambda: None)
        build_set = MagicMock()
        monkeypatch.setattr(precompute, '_build_chart_warm_set', build_set)
        precompute._warm_chart_cache()
        build_set.assert_not_called()


class TestWarmChartCacheSkipIfFresh:
    def test_fresh_entry_skips_rebuild(self, monkeypatch, _warm_env):
        monkeypatch.setattr(precompute, '_build_chart_warm_set', lambda: [('BTC', '1D')])
        monkeypatch.setattr('carrac.caches.get_cached_chart', lambda *a, **k: ({'x': 1}, True))
        precompute._warm_chart_cache()
        _warm_env['refresh'].assert_not_called()
        _warm_env['lock'].assert_not_called()

    def test_stale_entry_triggers_rebuild(self, monkeypatch, _warm_env):
        monkeypatch.setattr(precompute, '_build_chart_warm_set', lambda: [('BTC', '1D')])
        monkeypatch.setattr('carrac.caches.get_cached_chart', lambda *a, **k: ({'x': 1}, False))
        precompute._warm_chart_cache()
        _warm_env['refresh'].assert_called_once()

    def test_absent_entry_triggers_rebuild(self, monkeypatch, _warm_env):
        monkeypatch.setattr(precompute, '_build_chart_warm_set', lambda: [('BTC', '1D')])
        monkeypatch.setattr('carrac.caches.get_cached_chart', lambda *a, **k: None)
        precompute._warm_chart_cache()
        _warm_env['refresh'].assert_called_once()

    def test_lock_contention_defers_without_rebuild(self, monkeypatch, _warm_env):
        monkeypatch.setattr(precompute, '_build_chart_warm_set', lambda: [('BTC', '1D')])
        monkeypatch.setattr('carrac.caches.get_cached_chart', lambda *a, **k: None)
        _warm_env['lock'].return_value = False
        precompute._warm_chart_cache()
        _warm_env['refresh'].assert_not_called()


class TestWarmChartCacheQueryShape:
    def test_intraday_uses_ltf_mode_and_expected_days(self, monkeypatch, _warm_env):
        monkeypatch.setattr(precompute, '_build_chart_warm_set', lambda: [('BTC', '1h')])
        monkeypatch.setattr('carrac.caches.get_cached_chart', lambda *a, **k: None)
        precompute._warm_chart_cache()
        app, symbol, query, cache_key = _warm_env['refresh'].call_args[0]
        assert symbol == 'BTC'
        assert 'mode=ltf' in query
        assert 'fmt=col' in query
        assert 'quant=' not in query
        assert f'days={precompute._CHART_WARM_LTF_DAYS}' in query
        assert cache_key.startswith('BTC:1h:col:b') and cache_key.endswith(':0')

    def test_daily_uses_htf_mode_and_expected_days(self, monkeypatch, _warm_env):
        monkeypatch.setattr(precompute, '_build_chart_warm_set', lambda: [('ETH', '1D')])
        monkeypatch.setattr('carrac.caches.get_cached_chart', lambda *a, **k: None)
        precompute._warm_chart_cache()
        app, symbol, query, cache_key = _warm_env['refresh'].call_args[0]
        assert symbol == 'ETH'
        assert 'mode=htf' in query
        assert f'days={precompute._CHART_WARM_HTF_DAYS}' in query
        assert cache_key == 'ETH:1D:col:0'


class TestWarmChartCacheBudget:
    def test_budget_cutoff_stops_early_and_logs(self, monkeypatch, _warm_env, caplog):
        pairs = [('BTC', '1D'), ('ETH', '1D'), ('SOL', '1D')]
        monkeypatch.setattr(precompute, '_build_chart_warm_set', lambda: pairs)
        monkeypatch.setattr('carrac.caches.get_cached_chart', lambda *a, **k: None)
        monkeypatch.setenv('CHART_WARM_BUDGET_S', '10')

        clock = {'t': 0.0}
        monkeypatch.setattr(precompute.time, 'monotonic', lambda: clock['t'])

        def _refresh_side_effect(*a, **k):
            clock['t'] = 11.0  # blow past the 10s budget right after key #1

        _warm_env['refresh'].side_effect = _refresh_side_effect

        with caplog.at_level('INFO', logger='carrac.precompute'):
            precompute._warm_chart_cache()

        assert _warm_env['refresh'].call_count == 1
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            'warmed=1' in m and 'deferred=2' in m and 'budget_hit=True' in m
            for m in msgs
        ), msgs

    def test_no_budget_hit_when_fast(self, monkeypatch, _warm_env, caplog):
        pairs = [('BTC', '1D'), ('ETH', '1D')]
        monkeypatch.setattr(precompute, '_build_chart_warm_set', lambda: pairs)
        monkeypatch.setattr('carrac.caches.get_cached_chart', lambda *a, **k: None)
        with caplog.at_level('INFO', logger='carrac.precompute'):
            precompute._warm_chart_cache()
        assert _warm_env['refresh'].call_count == 2
        msgs = [r.getMessage() for r in caplog.records]
        assert any('warmed=2' in m and 'budget_hit=False' in m for m in msgs), msgs
