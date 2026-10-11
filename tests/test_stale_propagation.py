"""Staleness must reach the surface, not stop at precompute.

Splicing a live price onto dead history is prevented elsewhere.
That does not stop the *interpretation* of dead history being published as a
current read.

Observed on the live MCP server for MKR, 57 days with no bar:

    price:          1318.29      <- live, genuinely current
    facts.price:    1499.0       <- the last close, 57 days old
    facts.rsi:      6.1 "oversold"
    decisive_close: 7 entries, every one "confirmed": true
    key_levels:     pdh/pdl/mh/ml ALL 1499.0
    as_of:          <now>   <- stamped fresh

Nothing in that payload says the history is dead. `data_stale` was ALREADY
sitting in the HTF cache for MKR and KAS — every serving surface simply ignored
it. These tests fix the flag's readers, not its writer.

The rule these encode: an asset with no recent bars may still report its live
price, and must report NOTHING derived from the missing ones.
"""
from __future__ import annotations

import pandas as pd
import pytest
from datetime import datetime


NOW = datetime(2026, 8, 5)

# The staleness block as it travels from precompute to every surface.
MKR_STALE = {
    'stale': True,
    'last_bar_date': '2026-06-09',
    'age_days': 57,
    'reason': 'no_recent_bars',
}


def _frozen_flatline():
    """MKR as it actually was: identical bars, last one 2026-06-09.

    Real volumes on purpose. These rows stand for history that
    STOPPED — genuine bars nothing has been added to since. Written with
    volume 0 they were indistinguishable from the injected zero-volume rows
    that get persisted behind a dead feed, and once ``_last_real_bar`` learned
    to tell those apart this frame contained no real bars at all, so the
    describer correctly reported "no history" and lost the 2026-06-09 date
    the assertions below are about. The flat PRICES are the real shape; the
    zero volume never was.
    """
    return pd.DataFrame(
        {"open": [1499.0] * 4, "high": [1499.0] * 4, "low": [1499.0] * 4,
         "close": [1499.0] * 4, "volume": [812.0, 640.5, 733.25, 918.0]},
        index=pd.to_datetime(["2026-06-06", "2026-06-07", "2026-06-08", "2026-06-09"]),
    )


def _fresh_series():
    return pd.DataFrame(
        {"open": [62000.0, 62500.0, 63000.0], "high": [62800.0, 63200.0, 63400.0],
         "low": [61500.0, 62100.0, 62600.0], "close": [62500.0, 63000.0, 62900.0],
         "volume": [1000.0, 1100.0, 900.0]},
        index=pd.to_datetime(["2026-08-02", "2026-08-03", "2026-08-04"]),
    )


# ─────────────────────────────────────────────────────────────────────────
# 1. The describer — one place that turns a DataFrame into a shippable block
# ─────────────────────────────────────────────────────────────────────────
class TestDescribeStaleness:
    def test_stale_history_is_described_with_date_and_age(self):
        from carrac.precompute import describe_staleness

        # ZZSTALE, not MKR: MKR is now on instrument_master.LIFECYCLE_OVERRIDES, so
        # describe_staleness answers it from reference data before looking at a
        # bar. KAS was later curated too —
        # real tickers rot as stand-ins, so this one is fictional.
        # The rule under test is not symbol-specific.
        block = describe_staleness(_frozen_flatline(), 'ZZSTALE', now=NOW)
        assert block is not None
        assert block['stale'] is True
        assert block['last_bar_date'] == '2026-06-09'
        assert block['age_days'] == 57
        assert 'note' in block and block['note'], "the block must explain itself to an LLM"

    def test_fresh_history_describes_nothing(self):
        from carrac.precompute import describe_staleness
        assert describe_staleness(_fresh_series(), 'BTC', now=NOW) is None

    def test_empty_history_is_stale_without_a_date(self):
        from carrac.precompute import describe_staleness

        block = describe_staleness(pd.DataFrame(), 'BTC', now=NOW)
        assert block is not None and block['stale'] is True
        assert block.get('last_bar_date') is None

    def test_agrees_with_the_existing_boolean_gate(self):
        """describe_staleness and _is_history_stale must never disagree —
        two staleness definitions is how this bug class comes back."""
        from carrac.precompute import describe_staleness, _is_history_stale

        for df in (_frozen_flatline(), _fresh_series(), pd.DataFrame()):
            assert (describe_staleness(df, 'ZZSTALE', now=NOW) is not None) == _is_history_stale(df, 'ZZSTALE', now=NOW)
        # ...and the invariant must hold for a curated-delisted symbol too, which
        # is why _is_history_stale carries the same check rather than only
        # describe_staleness. database.py's live-price injection guard calls the
        # boolean one.
        for df in (_frozen_flatline(), _fresh_series(), pd.DataFrame()):
            assert (describe_staleness(df, 'MKR', now=NOW) is not None) == _is_history_stale(df, 'MKR', now=NOW)


# ─────────────────────────────────────────────────────────────────────────
# 2. The facts bundle — the surface that published the fabrication
# ─────────────────────────────────────────────────────────────────────────
def _stale_ctx():
    from carrac.facts.wire import build_facts_ctx
    df = _frozen_flatline()
    return build_facts_ctx(
        "MKR", df, 1499.0,
        key_levels={'pdh': 1499.0, 'pdl': 1499.0, 'mh': 1499.0, 'ml': 1499.0},
        rsi={'value': 6.1, 'zone': 'oversold'},
        daily_closes=list(df['close']),
        as_of='2026-08-05T18:34:56Z',
        weekly_df=df,
        data_stale=MKR_STALE,
    )


class TestBuildFactsCtxCarriesStaleness:
    def test_ctx_carries_the_block(self):
        assert _stale_ctx()['data_stale'] == MKR_STALE

    def test_fresh_ctx_has_no_stale_block(self):
        from carrac.facts.wire import build_facts_ctx
        ctx = build_facts_ctx("BTC", _fresh_series(), 62900.0)
        assert not ctx.get('data_stale')


class TestStaleBundleCarriesNoDerivedFacts:
    """The MKR payload, field by field. Each assertion is one thing an LLM
    quoted verbatim off dead bars."""

    @pytest.fixture
    def bundle(self):
        from carrac.facts.bundle import build_asset_facts
        return build_asset_facts("MKR", _stale_ctx())

    def test_the_bundle_says_it_is_stale(self, bundle):
        assert bundle['data_stale'] == MKR_STALE

    def test_identity_survives(self, bundle):
        assert bundle['symbol'] == "MKR"
        assert bundle['as_of'] == '2026-08-05T18:34:56Z'

    @pytest.mark.parametrize("field", [
        'rsi',              # 6.1 "oversold" — from 4 identical closes
        'decisive_close',   # 7 x confirmed:true — against levels that ARE the close
        'streaks',          # "testing pdh for 9 bars" — the bars do not exist
        'levels',           # pdh/pdl/mh/ml all 1499.0
        'ma_stack',         # "bearish_partial, bars_held 9"
        'stretch',          # mayer 0.994 off a flat series
        'candle',           # body 0.0, all wicks 0.0
        'range_pctile',     # pct 10 of a 90-day window with 4 bars in it
        'weekly',           # the same fabrication, repeated
        'price',            # 1499.0 — two months out of date, published as price
    ])
    def test_derived_field_is_absent(self, bundle, field):
        assert field not in bundle, (
            f"{field!r} is derived from bars that do not exist — "
            f"got {bundle.get(field)!r}"
        )

    def test_fresh_asset_keeps_every_derived_fact(self):
        """Guard against fixing the leak by emptying the feature."""
        from carrac.facts.wire import build_facts_ctx
        from carrac.facts.bundle import build_asset_facts

        df = _fresh_series()
        ctx = build_facts_ctx(
            "BTC", df, 62900.0,
            key_levels={'pdh': 63400.0, 'pdl': 61500.0},
            rsi={'value': 55.0, 'zone': 'neutral'},
            daily_closes=list(df['close']),
            as_of='2026-08-05T18:34:56Z',
        )
        bundle = build_asset_facts("BTC", ctx)
        assert 'data_stale' not in bundle
        assert bundle['price'] == 62900.0
        assert bundle['rsi'] == {'value': 55.0, 'zone': 'neutral'}
        assert 'decisive_close' in bundle and 'levels' in bundle


# ─────────────────────────────────────────────────────────────────────────
# 3. The scanner — KAS ranked normally with full evidence, 57 days dead
# ─────────────────────────────────────────────────────────────────────────
def _scored(total):
    return {'total': total, 'verdict': 'watch', 'criteria': {}, 'vetoes': []}


class TestScannerEngineCarriesStaleness:
    @pytest.fixture
    def scored_results(self, monkeypatch):
        from carrac.scanner.scorer import engine

        monkeypatch.setattr(engine, 'score_asset',
                            lambda asset, macro, side, event_risk=None: _scored(6))
        monkeypatch.setattr(engine, '_build_macro_data', lambda: {})
        monkeypatch.setattr(engine, '_event_risk_config', lambda: (24, False))

        assets = [
            {'symbol': 'BTC', 'type': 'crypto', 'latest_price': 64697.98},
            {'symbol': 'ZZSTALE', 'type': 'crypto', 'latest_price': 0.03217,
             'data_stale': MKR_STALE},
        ]
        out = engine.score_all_assets(assets, macro_data={})
        return {r['symbol']: r for r in out['results']}

    def test_stale_asset_result_carries_the_block(self, scored_results):
        assert scored_results['ZZSTALE']['data_stale'] == MKR_STALE

    def test_fresh_asset_result_is_unchanged(self, scored_results):
        assert 'data_stale' not in scored_results['BTC']


class TestEvidenceScannerSurfacesStaleness:
    def test_block_reaches_the_agent_shape(self):
        from carrac.formatters import evidence_scanner

        out = evidence_scanner({'results': [
            {'symbol': 'ZZSTALE', 'price': 0.0262, 'long': _scored(6), 'short': _scored(2),
             'data_stale': MKR_STALE},
        ]})
        assert out['results'][0]['data_stale'] == MKR_STALE

    def test_fresh_rows_gain_no_key(self):
        from carrac.formatters import evidence_scanner

        out = evidence_scanner({'results': [
            {'symbol': 'BTC', 'price': 64697.98, 'long': _scored(6), 'short': _scored(2)},
        ]})
        assert 'data_stale' not in out['results'][0]


class TestScreenExcludesStaleAssets:
    """A screen is a ranked list of what the market is doing. A fossil is not
    an observation — but dropping it silently is the banned move (the 'no silent caps'
    rule), so the payload must name what it removed."""

    @pytest.fixture(autouse=True)
    def _cache(self, monkeypatch):
        payload = {
            'results': [
                {'symbol': 'ZZSTALE', 'price': 0.0262, 'long': _scored(12), 'short': _scored(0),
                 'data_stale': MKR_STALE},
                {'symbol': 'MKR', 'price': 1318.29, 'long': _scored(11), 'short': _scored(1),
                 'data_stale': MKR_STALE},
                {'symbol': 'BTC', 'price': 64697.98, 'long': _scored(6), 'short': _scored(2)},
            ],
            'macro': {}, 'computed_at': '2026-08-05T18:00:00Z',
        }
        monkeypatch.setattr('carrac.caches.get_cached_scanner',
                            lambda *a, **k: dict(payload))
        monkeypatch.setattr('carrac.mcp.runtime.get_mcp_caller_tier', lambda: 'member')

    def test_screen_drops_stale_rows(self):
        from carrac.mcp.tools.scanner import scanner_get_screen
        out = scanner_get_screen({})
        assert [r['symbol'] for r in out['results']] == ['BTC']

    def test_screen_names_what_it_dropped(self):
        from carrac.mcp.tools.scanner import scanner_get_screen
        out = scanner_get_screen({})
        assert sorted(out['excluded_stale']) == ['MKR', 'ZZSTALE']

    def test_stale_rows_cannot_win_the_ranking(self):
        """KAS scores 12/12 off dead bars — must not rank on dead bars."""
        from carrac.mcp.tools.scanner import scanner_get_screen
        out = scanner_get_screen({'min_conditions': 10})
        assert out['results'] == []
        assert sorted(out['excluded_stale']) == ['MKR', 'ZZSTALE']

    def test_overview_has_the_same_blind_spot_closed(self):
        """Sibling sweep — market_get_overview reads the same cache."""
        from carrac.mcp.tools.market import market_get_overview
        out = market_get_overview({})
        assert [r['symbol'] for r in out['results']] == ['BTC']
        assert sorted(out['excluded_stale']) == ['MKR', 'ZZSTALE']

    def test_free_tier_cap_still_applies(self, monkeypatch):
        monkeypatch.setattr('carrac.mcp.runtime.get_mcp_caller_tier', lambda: 'free')
        from carrac.mcp.tools.scanner import scanner_get_screen
        out = scanner_get_screen({})
        assert len(out['results']) <= 5


# ─────────────────────────────────────────────────────────────────────────
# 4. market_get_detail — the exact call that produced the fabrication
# ─────────────────────────────────────────────────────────────────────────
class TestAssetDetailMarksStale:
    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch):
        monkeypatch.setattr(
            'carrac.composite.assemblers.assemble_asset_detail',
            lambda symbol, user_id=None: {
                'symbol': 'ZZSTALE',
                'price': 1318.29,
                'change_24h': 2.7,
                'data_stale': MKR_STALE,
                'technicals': {'consensio': {'1d': 0}, 'rsi_weekly': 42.1},
                'scanner': {'long': {'score': 6}, 'short': {'score': 4}},
            },
        )
        monkeypatch.setattr(
            'carrac.facts.wire.get_asset_facts',
            lambda sym, r=None: {'symbol': 'ZZSTALE', 'data_stale': MKR_STALE},
        )

    @pytest.fixture
    def result(self):
        # KAS rather than MKR — see TestDescribeStaleness above. MKR is now
        # curated-delisted, and this surface deliberately overrides an inherited
        # block with the delisted one, which is asserted in
        # tests/test_instrument_lifecycle.py rather than contradicted here.
        from carrac.mcp.tools.market import market_get_detail
        return market_get_detail({'symbol': 'ZZSTALE'})

    def test_the_live_price_survives(self, result):
        """The one number that is genuinely known. Erroring the whole tool
        would throw it away."""
        assert result['price'] == 1318.29
        assert result['change_24h'] == 2.7

    def test_staleness_is_stated(self, result):
        assert result['data_stale'] == MKR_STALE

    @pytest.mark.parametrize("field", ['technicals', 'scanner', 'facts'])
    def test_derived_block_is_nulled(self, result, field):
        assert result[field] is None, \
            f"{field} is computed from bars that stopped on 2026-06-09"


class TestAssetDetailAssemblerCarriesFlag:
    def test_htf_data_stale_reaches_the_assembled_result(self, monkeypatch):
        """Additive only — the HTTP composite routes share this shape, so the
        flag is carried, not acted on, at this layer."""
        from carrac.composite import assemblers

        monkeypatch.setattr(assemblers, '_get_htf_by_symbol',
                            lambda symbols=None: {'MKR': {'symbol': 'MKR',
                                                          'latest_price': 1499.0,
                                                          'data_stale': MKR_STALE}})
        monkeypatch.setattr(assemblers, '_get_scanner_by_symbol', lambda symbols=None: {})
        monkeypatch.setattr(assemblers, '_get_prices',
                            lambda: {'MKR': {'price': 1318.29, 'change_pct': 2.7}})

        out = assemblers.assemble_asset_detail('MKR', user_id=None)
        assert out['data_stale'] == MKR_STALE
        assert out['price'] == 1318.29, "live price still comes from the prices cache"

    def test_compact_formatter_carries_the_flag(self):
        from carrac.composite.formatters import compact_asset_detail

        out = compact_asset_detail({'symbol': 'MKR', 'price': 1318.29,
                                    'data_stale': MKR_STALE})
        assert out['data_stale'] == MKR_STALE

    def test_compact_formatter_omits_the_key_when_fresh(self):
        from carrac.composite.formatters import compact_asset_detail
        assert 'data_stale' not in compact_asset_detail({'symbol': 'BTC', 'price': 64697.98})


# ─────────────────────────────────────────────────────────────────────────
# 5. Siblings that read the same caches
# ─────────────────────────────────────────────────────────────────────────
class TestChartMarksStale:
    @pytest.fixture(autouse=True)
    def _cache(self, monkeypatch):
        from carrac import precompute
        htf = {
            'assets': [{
                'symbol': 'MKR', 'latest_price': 1499.0, 'change_pct': 0.0,
                'consensio': {'total': {'positions': 0}, 'signal': 'neutral'},
                'rsi': 6.1, 'trend': 'bear', 'structure': 'down',
                'key_levels': {'pdh': 1499.0, 'pdl': 1499.0},
                'data_stale': MKR_STALE,
            }],
            'computed_at': '2026-08-05T18:00:00Z',
        }
        monkeypatch.setattr(precompute.response_cache, 'get',
                            lambda key, *a, **k: htf if key == 'htf' else None)
        monkeypatch.setattr('cache_manager.get_cached_crypto_prices',
                            lambda *a, **k: {'MKR': {'price': 1318.29, 'change_pct': 2.7}})

    def test_chart_states_staleness(self):
        from carrac.mcp.tools.market import market_get_chart
        assert market_get_chart({'symbol': 'MKR'})['data_stale'] == MKR_STALE

    @pytest.mark.parametrize("field", ['rsi', 'trend', 'structure', 'key_levels', 'consensio'])
    def test_chart_drops_derived_fields(self, field):
        from carrac.mcp.tools.market import market_get_chart
        assert market_get_chart({'symbol': 'MKR'}).get(field) in (None, {}, [])


class TestReportsSkipStaleAssets:
    """The published reports read the same bundles. A dead asset must not
    occupy a spotlight slot — and must not leave an empty heading behind
    where its card would have been."""

    _STALE_BUNDLE = {'symbol': 'MKR', 'as_of': '2026-08-05T18:34:56Z',
                     'data_stale': MKR_STALE}
    _LIVE_BUNDLE = {
        'symbol': 'BTC', 'as_of': '2026-08-05T18:34:56Z', 'price': 64697.98,
        'rsi': {'value': 55.0, 'zone': 'neutral'},
        'levels': {'clusters': [], 'key_levels': {'pdh': 65000.0}},
        'ma_stack': {'state': 'bullish', 'bars_held': 4},
        'streaks': [], 'decisive_close': [],
    }

    def test_stale_asset_gets_no_card_at_all(self):
        from morning_engine.ta_cards import build_ta_cards_block

        out = build_ta_cards_block(
            ['MKR', 'BTC'],
            get_facts=lambda s: {'MKR': self._STALE_BUNDLE,
                                 'BTC': self._LIVE_BUNDLE}[s],
        )
        assert 'MKR' not in out, "a dead asset took a spotlight slot"
        assert 'BTC' in out, "the live asset must still get its card"

    def test_an_all_stale_run_renders_nothing(self):
        """Not even the heading — a spotlight section with no spotlight is
        how an empty '### MKR' shipped into a report."""
        from morning_engine.ta_cards import build_ta_cards_block

        out = build_ta_cards_block(['MKR'], get_facts=lambda s: self._STALE_BUNDLE)
        assert out == ""

    def test_stale_asset_contributes_no_sentence(self):
        """Guards what already works: the salience ranker cannot
        find a sentence in a bundle with no facts, so a dead asset can never
        become the editorial spotlight."""
        from carrac.facts.sentences import asset_sentences
        assert asset_sentences(self._STALE_BUNDLE, k=3) == []


class TestSearchMarksStale:
    def test_curated_stale_symbol_is_flagged_before_it_is_asked_about(self, monkeypatch):
        """The cheapest possible save: the agent learns MKR is dead at lookup
        time, before it spends a market_get_detail call on it."""
        from carrac.mcp.tools import market as market_tools

        class _DB:
            def get_all_symbols(self):
                return ['MKR', 'BTC']

            def get_asset_info(self, s):
                return {'name': {'MKR': 'Maker', 'BTC': 'Bitcoin'}[s], 'type': 'crypto'}

        class _Services:
            db = _DB()

        monkeypatch.setattr('carrac.services.get_services', lambda: _Services())
        monkeypatch.setattr(market_tools, '_stale_symbols', lambda: {'MKR': MKR_STALE})

        rows = {r['symbol']: r for r in market_tools.market_search_symbol(
            {'query': 'M'})['results']}
        assert rows['MKR']['data_stale'] == MKR_STALE

    def test_fresh_symbol_is_not_flagged(self, monkeypatch):
        from carrac.mcp.tools import market as market_tools

        class _DB:
            def get_all_symbols(self):
                return ['BTC']

            def get_asset_info(self, s):
                return {'name': 'Bitcoin', 'type': 'crypto'}

        class _Services:
            db = _DB()

        monkeypatch.setattr('carrac.services.get_services', lambda: _Services())
        monkeypatch.setattr(market_tools, '_stale_symbols', lambda: {'MKR': MKR_STALE})

        rows = market_tools.market_search_symbol({'query': 'BTC'})['results']
        assert 'data_stale' not in rows[0]
