"""Tests for composite endpoints."""

import pytest


# ── Assembler unit tests (no Flask context needed) ────────────────

class TestSafeWrapper:
    """Test the _safe() graceful degradation helper."""

    def test_safe_returns_value_on_success(self):
        from carrac.composite.assemblers import _safe
        avail = {}
        result = _safe('test', lambda: 42, avail)
        assert result == 42
        assert avail['test'] == 'ok'

    def test_safe_returns_fallback_on_error(self):
        from carrac.composite.assemblers import _safe
        avail = {}
        result = _safe('test', lambda: 1 / 0, avail, fallback='nope')
        assert result == 'nope'
        assert avail['test'] == 'error'

    def test_safe_default_fallback_for_list_sources(self):
        from carrac.composite.assemblers import _safe
        avail = {}
        result = _safe('plays', lambda: 1 / 0, avail)
        assert result == []

    def test_safe_default_fallback_for_dict_sources(self):
        from carrac.composite.assemblers import _safe
        avail = {}
        result = _safe('htf', lambda: 1 / 0, avail)
        assert result == {}


class TestExtractHelpers:
    """Test data extraction helpers."""

    def test_extract_market_from_prices(self):
        from carrac.composite.assemblers import _extract_market
        prices = {'BTC': {'price': 84000, 'change_pct': 1.5}}
        result = _extract_market('BTC', {}, prices)
        assert result['price'] == 84000
        assert result['change_24h'] == 1.5

    def test_extract_market_from_htf_fallback(self):
        from carrac.composite.assemblers import _extract_market
        htf = {'BTC': {'latest_price': 83000, 'change_24h': -0.5}}
        result = _extract_market('BTC', htf, {})
        assert result['price'] == 83000

    def test_extract_scanner(self):
        from carrac.composite.assemblers import _extract_scanner
        scanner = {'BTC': {'long': {'total': 10, 'verdict': 'STRONG'}, 'short': {'total': 3, 'verdict': 'AVOID'}, 'best_verdict': 'STRONG'}}
        result = _extract_scanner('BTC', scanner)
        assert result['long']['score'] == 10
        assert result['long']['verdict'] == 'STRONG'
        assert result['best_verdict'] == 'STRONG'

    def test_extract_scanner_missing(self):
        from carrac.composite.assemblers import _extract_scanner
        result = _extract_scanner('XYZ', {})
        assert result is None

    def test_extract_technicals(self):
        from carrac.composite.assemblers import _extract_technicals
        htf = {'BTC': {
            'consensio': {
                'total': {'positions': 5, 'max_positions': 6},
                '1D': {'signal': 'bullish'},
                '3D': {'signal': 'bullish'},
                '1W': {'signal': 'neutral'},
            },
            'weekly_rsi': 62.5,
            'ema_ribbon': {'trend': 'bullish'},
            'adx': {'adx': 28.3},
        }}
        result = _extract_technicals('BTC', htf)
        assert result['consensio']['1d'] == 'bullish'
        assert result['consensio_aligned'] is True
        assert result['rsi_weekly'] == 62.5
        assert result['ema_ribbon_trend'] == 'bullish'
        assert result['adx'] == 28.3

    def test_extract_levels_reads_nested_key_levels(self):
        """precompute stores pdh/pdl/pwh/pwl nested under
        asset_result['key_levels'] (carrac/precompute.py::_compute_single_asset_ltf), not
        top-level — _extract_levels must read from there."""
        from carrac.composite.assemblers import _extract_levels
        htf = {'BTC': {
            'key_levels': {'pdh': 85000.0, 'pdl': 82000.0, 'pwh': 88000.0, 'pwl': 80000.0},
        }}
        result = _extract_levels('BTC', htf)
        assert result['pdh'] == 85000.0
        assert result['pdl'] == 82000.0
        assert result['pwh'] == 88000.0
        assert result['pwl'] == 80000.0

    def test_extract_levels_falls_back_to_top_level(self):
        """Top-level pdh/pdl/pwh/pwl (if ever present) still work as a
        fallback."""
        from carrac.composite.assemblers import _extract_levels
        htf = {'BTC': {'pdh': 85000.0, 'pdl': 82000.0}}
        result = _extract_levels('BTC', htf)
        assert result['pdh'] == 85000.0
        assert result['pdl'] == 82000.0

    def test_time_since(self):
        from carrac.composite.assemblers import _time_since
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(days=2, hours=5)).isoformat()
        result = _time_since(ts)
        assert '2d' in result


class TestBatchValidation:
    """Test batch parameter validation constants."""

    def test_valid_blocks(self):
        from carrac.composite.assemblers import VALID_BLOCKS
        assert 'market' in VALID_BLOCKS
        assert 'scanner' in VALID_BLOCKS
        assert 'sentiment' in VALID_BLOCKS
        assert 'alerts' in VALID_BLOCKS
        assert 'plays' in VALID_BLOCKS
        assert 'positions' in VALID_BLOCKS
        assert 'drawings' in VALID_BLOCKS

    def test_max_batch_assets(self):
        from carrac.composite.assemblers import MAX_BATCH_ASSETS
        assert MAX_BATCH_ASSETS == 10


# ── Formatter tests ───────────────────────────────────────────────

class TestFormatters:
    """Test compact formatters."""

    def test_compact_asset_detail(self):
        from carrac.composite.formatters import compact_asset_detail
        full = {
            'symbol': 'BTC', 'price': 84000, 'change_24h': 1.5,
            'technicals': {'consensio': {'1d': 'bull'}, 'consensio_aligned': True, 'rsi_weekly': 62, 'ema_ribbon_trend': 'bull'},
            'scanner': {'long': {'score': 10, 'verdict': 'STRONG'}},
            'sentiment': {'regime': 'Neutral'},
            'plays': [{'id': 1, 'title': 'Test', 'status': 'watching', 'direction': 'long'}],
            'alerts': [{'id': 'a1'}, {'id': 'a2'}],
            'drawings': [{'type': 'trendline'}],
            'positions': [{'asset': 'BTC', 'direction': 'long', 'unrealized_pnl_pct': 2.1, 'risk_status': 'profitable'}],
        }
        compact = compact_asset_detail(full)
        assert compact['symbol'] == 'BTC'
        assert compact['plays_count'] == 1
        assert compact['alerts_count'] == 2
        assert compact['drawings_count'] == 1
        assert compact['positions_count'] == 1
        assert 'technicals' in compact
        assert 'scanner' in compact

    def test_compact_live_positions(self):
        from carrac.composite.formatters import compact_live_positions
        full = {
            'positions': [{'asset': 'BTC', 'direction': 'long', 'entry_price': 82000,
                           'current_price': 84000, 'unrealized_pnl_pct': 2.4,
                           'current_rr': 0.8, 'risk_status': 'profitable',
                           'scanner_verdict': 'STRONG', 'linked_play': {'id': 1}}],
            'portfolio_heat': {'total_exposure': 1, 'long_count': 1, 'short_count': 0},
        }
        compact = compact_live_positions(full)
        assert len(compact['positions']) == 1
        assert compact['positions'][0]['pnl_pct'] == 2.4
        assert compact['heat']['total_exposure'] == 1

    def test_compact_batch(self):
        from carrac.composite.formatters import compact_batch
        full = {
            'assets': {
                'BTC': {
                    'market': {'price': 84000, 'change_24h': 1.5},
                    'scanner': {'long': {'score': 10}},
                    'alerts': [{'id': 'a1'}, {'id': 'a2'}],
                },
            },
            'global': {'sentiment': {'regime': 'Neutral'}},
        }
        compact = compact_batch(full)
        assert compact['assets']['BTC']['price'] == 84000
        assert compact['assets']['BTC']['alerts_count'] == 2
        assert compact['global']['sentiment']['regime'] == 'Neutral'


class TestBatchCarriesStaleness:
    """util_batch_query published verdicts off dead history.

    The guard computes the flag correctly, and the batch path must not throw it
    away. Example: an asset whose real bars stopped weeks ago gets a populated
    ``describe_staleness`` block, and ``scanner_get_screen`` / ``market_get_overview`` both listed it under
    ``excluded_stale`` — while ``util_batch_query`` returned a NEUTRAL 6/12
    short verdict with no marker anywhere in the payload.

    ``util_batch_query`` takes up to 10 assets and is sold as the efficient
    cross-asset call, so it is exactly what a caller reaches for when NOT
    looking closely at any single asset. The worst place to lose the one field
    that says "do not trust this".
    """

    _STALE = {
        'stale': True,
        'reason': 'no_recent_bars',
        'last_bar_date': '2026-05-25',
        'age_days': 78,
        'note': 'history stopped 78 days ago',
    }

    def test_compact_batch_keeps_the_marker_from_the_market_block(self):
        from carrac.composite.formatters import compact_batch
        full = {
            'assets': {
                'CHILLGUY': {
                    'market': {'price': 0.012699, 'change_24h': None,
                               'data_stale': self._STALE},
                    'scanner': {'short': {'score': 6, 'verdict': 'NEUTRAL'}},
                },
            },
        }
        entry = compact_batch(full)['assets']['CHILLGUY']
        assert entry['data_stale'] == self._STALE, (
            'compact_batch rebuilds the entry field by field; before that fix '
            'it copied price and change and silently dropped data_stale'
        )

    def test_scanner_only_call_still_carries_the_marker(self):
        """The boundary case, and the reason this is a sweep not a one-liner.

        ``assemble_batch`` only populates ``htf_by_symbol`` when 'market' is in
        the requested blocks, and ``data_stale`` is read from there — so with
        include='scanner' the flag does not exist at ANY layer, while
        ``_extract_scanner`` happily returns a verdict. Fixing only the
        compaction step would leave this path silently wrong.

        The scanner rows already carry the block (scanner/scorer/engine.py sets
        it per row), so the fix is to stop discarding it, not to plumb a new
        source through.
        """
        from carrac.composite.assemblers import _extract_scanner
        scanner = {
            'CHILLGUY': {
                'long': {'total': 4, 'verdict': 'WEAK'},
                'short': {'total': 6, 'verdict': 'NEUTRAL'},
                'best_verdict': 'NEUTRAL',
                'data_stale': self._STALE,
            },
        }
        result = _extract_scanner('CHILLGUY', scanner)
        assert result['best_verdict'] == 'NEUTRAL'
        assert result['data_stale'] == self._STALE

    def test_compact_batch_keeps_a_scanner_only_marker(self):
        from carrac.composite.formatters import compact_batch
        full = {
            'assets': {
                'CHILLGUY': {
                    'scanner': {'short': {'score': 6, 'verdict': 'NEUTRAL'},
                                'data_stale': self._STALE},
                },
            },
        }
        entry = compact_batch(full)['assets']['CHILLGUY']
        assert entry['data_stale'] == self._STALE

    def test_a_live_asset_gains_no_marker(self):
        """The control. A fix that flags everything is not a fix."""
        from carrac.composite.assemblers import _extract_scanner
        from carrac.composite.formatters import compact_batch

        scanner = {'BTC': {'long': {'total': 10, 'verdict': 'STRONG'},
                           'short': {'total': 3, 'verdict': 'AVOID'},
                           'best_verdict': 'STRONG'}}
        assert 'data_stale' not in _extract_scanner('BTC', scanner)

        full = {'assets': {'BTC': {'market': {'price': 84000, 'change_24h': 1.5},
                                   'scanner': {'long': {'score': 10}}}}}
        assert 'data_stale' not in compact_batch(full)['assets']['BTC']


class TestUserScopingInAssemblers:
    """Composite endpoints must never leak
    other users' plays/drawings when the caller's user_id resolves to None
    (e.g. shared-admin-key path or an auth failure).
    """

    def test_assemble_batch_returns_empty_plays_when_user_id_none(self, monkeypatch):
        """get_active_plays(None) returns ALL users; assembler must short-circuit."""
        from carrac.composite import assemblers

        # Sentinel: this should NEVER be called when user_id is None.
        def _leaky_helper(uid):
            raise AssertionError(f'get_active_plays was called with user_id={uid!r} — LEAK!')

        monkeypatch.setattr('carrac.plays.models.get_active_plays', _leaky_helper)

        # Call assemble_batch with user_id=None and 'plays' in blocks.
        result = assemblers.assemble_batch(['BTC'], ['plays'], user_id=None)

        assert result['assets']['BTC']['plays'] == []

    def test_assemble_batch_returns_empty_drawings_when_user_id_none(self, monkeypatch):
        """drawing_list(user_id=None) returns ALL; assembler must short-circuit."""
        from carrac.composite import assemblers

        class _Sentinel:
            def get_all_drawings_grouped(self, uid):
                raise AssertionError(f'get_all_drawings_grouped called with {uid!r} — LEAK!')

        monkeypatch.setattr(assemblers, '_get_content_db', lambda: _Sentinel())

        # No leak exception raised (sentinel asserts that). The call itself
        # is what matters — assemble_batch must not reach the drawing DB.
        assemblers.assemble_batch(['BTC'], ['drawings'], user_id=None)

    def test_asset_detail_returns_empty_drawings_when_user_id_none(self, monkeypatch):
        """assemble_asset_detail's drawings call is also guarded."""
        from carrac.composite import assemblers

        class _Sentinel:
            def get_all_drawings_grouped(self, uid):
                raise AssertionError(f'get_all_drawings_grouped called with {uid!r} — LEAK!')
            def get_open_trades(self, uid, **kw):
                return []

        monkeypatch.setattr(assemblers, '_get_content_db', lambda: _Sentinel())

        # Sentinel asserts the drawing DB is never called. The actual drawings
        # value is a formatter detail (empty list/dict/count) — what matters
        # is that we never reached the leaky helper.
        assemblers.assemble_asset_detail('BTC', user_id=None)


class TestCompositeUserResolution:
    """The composite router scopes data to the caller: a per-user API key wins
    over a session, and a caller with neither (the shared internal key) is
    refused rather than falling through to cross-user reads."""

    def test_api_key_user_wins(self):
        from carrac.fastapi_app.routers.composite import _resolve_user_id
        auth = {'source': 'api_key', 'user_id': 123, 'user': {'id': 999}}
        assert _resolve_user_id(auth) == 123

    def test_falls_back_to_session_user(self):
        from carrac.fastapi_app.routers.composite import _resolve_user_id
        assert _resolve_user_id({'source': 'session', 'user': {'id': 456}}) == 456

    def test_none_when_neither_is_bound(self):
        from carrac.fastapi_app.routers.composite import _resolve_user_id
        assert _resolve_user_id({'source': 'api_key', 'user_id': None}) is None

    def test_require_user_id_refuses_unbound_caller_with_401(self):
        from fastapi import HTTPException

        from carrac.fastapi_app.routers.composite import _require_user_id
        with pytest.raises(HTTPException) as exc:
            _require_user_id({'source': 'api_key', 'user_id': None})
        assert exc.value.status_code == 401

    def test_require_user_id_returns_bound_user(self):
        from carrac.fastapi_app.routers.composite import _require_user_id
        assert _require_user_id({'source': 'api_key', 'user_id': 99}) == 99


class TestScannerHtfPerSymbolReads:
    """_get_scanner_by_symbol / _get_htf_by_symbol read the
    per-symbol Redis hash first (HMGET, few-KB per symbol) and only fall
    back to the whole blob (10.8MB scanner / 2.1MB htf) when the hash is
    missing/empty. Output shape must be identical either way — the
    downstream _extract_market/_extract_scanner extraction logic gets the
    same per-symbol dicts regardless of which path served them.
    """

    # ── scanner ──────────────────────────────────────────────────────

    def test_scanner_prefers_hash_and_never_touches_blob(self, monkeypatch):
        from carrac.composite import assemblers

        def _leaky_blob():
            raise AssertionError('get_cached_scanner was called — hash path should short-circuit')

        monkeypatch.setattr(
            'carrac.caches.get_scanner_by_symbol_hash',
            lambda symbols: {'BTC': {'symbol': 'BTC', 'long': {'total': 8}}},
        )
        monkeypatch.setattr('carrac.caches.get_cached_scanner', _leaky_blob)

        result = assemblers._get_scanner_by_symbol(['BTC'])
        assert result == {'BTC': {'symbol': 'BTC', 'long': {'total': 8}}}

    def test_scanner_falls_back_to_blob_when_hash_empty(self, monkeypatch):
        from carrac.composite import assemblers

        blob = {'results': [
            {'symbol': 'BTC', 'long': {'total': 8}},
            {'symbol': 'ETH', 'long': {'total': 5}},
        ]}
        monkeypatch.setattr('carrac.caches.get_scanner_by_symbol_hash', lambda symbols: None)
        monkeypatch.setattr('carrac.caches.get_cached_scanner', lambda: blob)

        result = assemblers._get_scanner_by_symbol(['BTC', 'ETH'])
        assert result == {
            'BTC': {'symbol': 'BTC', 'long': {'total': 8}},
            'ETH': {'symbol': 'ETH', 'long': {'total': 5}},
        }

    def test_scanner_no_symbols_uses_blob_directly(self, monkeypatch):
        """Callers that don't know a bounded symbol set (symbols=None) skip
        the hash entirely — unchanged previous behavior."""
        from carrac.composite import assemblers

        blob = {'results': [{'symbol': 'BTC'}]}
        monkeypatch.setattr('carrac.caches.get_cached_scanner', lambda: blob)

        result = assemblers._get_scanner_by_symbol()
        assert result == {'BTC': {'symbol': 'BTC'}}

    def test_scanner_hash_and_blob_paths_are_identical_for_same_data(self, monkeypatch):
        """Parity: given the same underlying per-symbol data, the hash path
        and the blob-fallback path must produce byte-identical output."""
        from carrac.composite import assemblers

        per_symbol = {
            'BTC': {'symbol': 'BTC', 'long': {'total': 8, 'verdict': 'STRONG'},
                    'short': {'total': 2, 'verdict': 'AVOID'}, 'best_verdict': 'STRONG'},
            'ETH': {'symbol': 'ETH', 'long': {'total': 4, 'verdict': 'NEUTRAL'},
                    'short': {'total': 3, 'verdict': 'AVOID'}, 'best_verdict': 'NEUTRAL'},
        }

        # Hash path.
        monkeypatch.setattr('carrac.caches.get_scanner_by_symbol_hash', lambda symbols: dict(per_symbol))
        via_hash = assemblers._get_scanner_by_symbol(['BTC', 'ETH'])

        # Blob path (hash empty -> fallback), same underlying data.
        monkeypatch.setattr('carrac.caches.get_scanner_by_symbol_hash', lambda symbols: None)
        monkeypatch.setattr('carrac.caches.get_cached_scanner',
                             lambda: {'results': list(per_symbol.values())})
        via_blob = assemblers._get_scanner_by_symbol(['BTC', 'ETH'])

        assert via_hash == via_blob == per_symbol

    # ── htf ──────────────────────────────────────────────────────────

    def test_htf_prefers_hash_and_never_touches_response_cache(self, monkeypatch):
        """Note: _get_htf_by_symbol's blob fallback is wrapped in a bare
        try/except (pre-existing behavior, unchanged) — a spy that raises
        would be silently swallowed and masked by the empty-dict return, so
        this uses a call-counter instead of a raising sentinel."""
        from carrac.composite import assemblers

        calls = {'n': 0}

        class _SpyResponseCache:
            def get(self, mode):
                calls['n'] += 1
                return {'assets': []}

        monkeypatch.setattr(
            'carrac.caches.get_htf_by_symbol_hash',
            lambda symbols: {'BTC': {'symbol': 'BTC', 'latest_price': 80000.0}},
        )
        monkeypatch.setattr(assemblers, '_get_response_cache', lambda: _SpyResponseCache())

        result = assemblers._get_htf_by_symbol(['BTC'])
        assert result == {'BTC': {'symbol': 'BTC', 'latest_price': 80000.0}}
        assert calls['n'] == 0, 'response_cache.get() was called despite the hash hit — blob path not short-circuited'

    def test_htf_falls_back_to_blob_when_hash_empty(self, monkeypatch):
        from carrac.composite import assemblers

        class _FakeResponseCache:
            def get(self, mode):
                assert mode == 'htf'
                return {'assets': [
                    {'symbol': 'BTC', 'latest_price': 80000.0},
                    {'symbol': 'ETH', 'latest_price': 4000.0},
                ]}

        monkeypatch.setattr('carrac.caches.get_htf_by_symbol_hash', lambda symbols: None)
        monkeypatch.setattr(assemblers, '_get_response_cache', lambda: _FakeResponseCache())

        result = assemblers._get_htf_by_symbol(['BTC', 'ETH'])
        assert result == {
            'BTC': {'symbol': 'BTC', 'latest_price': 80000.0},
            'ETH': {'symbol': 'ETH', 'latest_price': 4000.0},
        }

    def test_htf_hash_and_blob_paths_are_identical_for_same_data(self, monkeypatch):
        from carrac.composite import assemblers

        per_symbol = {
            'BTC': {'symbol': 'BTC', 'latest_price': 80000.0, 'weekly_rsi': 62.5},
        }

        monkeypatch.setattr('carrac.caches.get_htf_by_symbol_hash', lambda symbols: dict(per_symbol))
        via_hash = assemblers._get_htf_by_symbol(['BTC'])

        class _FakeResponseCache:
            def get(self, mode):
                return {'assets': list(per_symbol.values())}

        monkeypatch.setattr('carrac.caches.get_htf_by_symbol_hash', lambda symbols: None)
        monkeypatch.setattr(assemblers, '_get_response_cache', lambda: _FakeResponseCache())
        via_blob = assemblers._get_htf_by_symbol(['BTC'])

        assert via_hash == via_blob == per_symbol

    # ── assemble_batch end-to-end parity ────────────────────────────

    def test_assemble_batch_identical_via_hash_or_blob(self, monkeypatch):
        """The actual /api/batch assembler (Flask-side, used by the MCP
        util_batch_query tool) must produce the same 'market'/'scanner' blocks
        whether the per-symbol hash or the blob fallback served the data."""
        from carrac.composite import assemblers

        scanner_per_symbol = {
            'BTC': {'symbol': 'BTC', 'long': {'total': 8, 'verdict': 'STRONG'},
                    'short': {'total': 2, 'verdict': 'AVOID'}, 'best_verdict': 'STRONG'},
        }
        htf_per_symbol = {
            'BTC': {'symbol': 'BTC', 'latest_price': 80000.0, 'change_pct': 1.1},
        }
        monkeypatch.setattr(assemblers, '_get_prices', lambda: {})

        # Hash path.
        monkeypatch.setattr('carrac.caches.get_scanner_by_symbol_hash',
                             lambda symbols: dict(scanner_per_symbol))
        monkeypatch.setattr('carrac.caches.get_htf_by_symbol_hash',
                             lambda symbols: dict(htf_per_symbol))
        via_hash = assemblers.assemble_batch(['BTC'], ['market', 'scanner'], user_id=None)

        # Blob path.
        monkeypatch.setattr('carrac.caches.get_scanner_by_symbol_hash', lambda symbols: None)
        monkeypatch.setattr('carrac.caches.get_cached_scanner',
                             lambda: {'results': list(scanner_per_symbol.values())})
        monkeypatch.setattr('carrac.caches.get_htf_by_symbol_hash', lambda symbols: None)

        class _FakeResponseCache:
            def get(self, mode):
                return {'assets': list(htf_per_symbol.values())}

        monkeypatch.setattr(assemblers, '_get_response_cache', lambda: _FakeResponseCache())
        via_blob = assemblers.assemble_batch(['BTC'], ['market', 'scanner'], user_id=None)

        assert via_hash['assets'] == via_blob['assets']
        assert via_hash['assets']['BTC']['scanner']['long']['score'] == 8
        assert via_hash['assets']['BTC']['market']['price'] == 80000.0


class TestOpenAPIComposite:
    """Test that composite endpoints are in the OpenAPI spec."""

    def setup_method(self):
        from carrac.openapi import reset_spec_cache
        reset_spec_cache()

    def test_composite_paths_in_spec(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        paths = spec['paths']
        assert '/api/asset/{symbol}/detail' in paths
        assert '/api/positions/live' in paths
        assert '/api/batch' in paths

    def test_composite_tag_in_spec(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        tags = {t['name'] for t in spec['tags']}
        assert 'Composite' in tags

    def test_spec_path_count_increased(self):
        from carrac.openapi import build_spec
        spec = build_spec()
        assert len(spec['paths']) >= 48  # 45 + 3 composite
