"""Staleness must propagate to every symbol-taking read-only tool.

Every symbol-taking read-only tool marks stale or missing data:

  scanner_get_confluence_zones("MKR"): levels from a series that stopped
       months ago, next to a live price, carry a staleness marker.

  market_get_detail("FTM"): an asset with ZERO OHLCV rows has no `data_stale`
       flag in the HTF cache (precompute writes it), so "no data" must be
       reported as its own state rather than falling through as "fresh".

The staleness predicate is reachable from anything that publishes a technical
read (no per-tool allowlist), and absence of data is a state, not a gap.
"""
from __future__ import annotations

import pytest


MKR_STALE = {
    'stale': True,
    'reason': 'no_recent_bars',
    'last_bar_date': '2026-06-09',
    'age_days': 57,
    'note': 'No price data has been collected for this asset since 2026-06-09.',
}


# ─────────────────────────────────────────────────────────────────────────
# Confluence zones
# ─────────────────────────────────────────────────────────────────────────
class TestConfluenceZonesRespectStaleness:
    _PAYLOAD = {
        'symbol': 'MKR',
        'price': 1298.85,
        'supports': [{'price': 1451.0, 'label': 'SW_PL', 'label_friendly': 'Prev Swing Low',
                      'sources': ['structure'], 'members': ['SW_PL'], 'strength': 1,
                      'tier': 'weak', 'side': 'support'}],
        'resistances': [{'price': 1617.6, 'label': 'SW_L', 'label_friendly': 'Swing Low',
                         'sources': ['structure'], 'members': ['SW_L'], 'strength': 1,
                         'tier': 'weak', 'side': 'resistance'}],
        'all': [1, 2],
    }

    @pytest.fixture
    def call(self, monkeypatch):
        def _call(symbol, stale_block):
            from carrac.scanner import confluence_query
            monkeypatch.setattr(
                confluence_query, 'get_symbol_confluence',
                lambda s: (dict(self._PAYLOAD, symbol=s), 'ok'),
            )
            monkeypatch.setattr(
                'carrac.mcp.tools.scanner._staleness_for_symbol',
                lambda s: stale_block,
            )
            from carrac.mcp.tools.scanner import scanner_get_confluence_zones
            return scanner_get_confluence_zones({'symbol': symbol})
        return _call

    def test_stale_symbol_states_it(self, call):
        assert call('MKR', MKR_STALE)['data_stale'] == MKR_STALE

    def test_stale_symbol_publishes_no_zones(self, call):
        """The levels ARE the fabrication — 1451.0 is a swing low from a series
        that stopped two months ago, offered against a live price."""
        out = call('MKR', MKR_STALE)
        assert out['supports'] == []
        assert out['resistances'] == []
        assert out['total_zones'] == 0

    def test_stale_symbol_keeps_its_live_price(self, call):
        assert call('MKR', MKR_STALE)['price'] == 1298.85

    def test_fresh_symbol_is_untouched(self, call):
        """Guard against fixing the leak by emptying the tool."""
        out = call('BTC', None)
        assert 'data_stale' not in out
        assert len(out['supports']) == 1 and len(out['resistances']) == 1
        assert out['total_zones'] == 2


# ─────────────────────────────────────────────────────────────────────────
# No history at all
# ─────────────────────────────────────────────────────────────────────────
class TestNoHistoryIsAState:
    def test_absent_from_the_universe_yields_a_stale_block(self):
        from carrac.staleness import no_history_block, history_is_dead

        b = no_history_block('FTM')
        assert b['stale'] is True
        assert b['reason'] == 'no_history'
        assert 'FTM' in b['note'] or 'no price history' in b['note'].lower()
        assert history_is_dead(b), "no_history must suppress like dead history does"

    def test_asset_detail_flags_a_symbol_with_a_price_but_no_technicals(self, monkeypatch):
        """The FTM shape exactly: the prices feed knows it, precompute does not."""
        monkeypatch.setattr(
            'carrac.composite.assemblers.assemble_asset_detail',
            lambda symbol, user_id=None: {
                'symbol': 'FTM', 'price': 0.4657, 'change_24h': 362.1,
                'technicals': None, 'scanner': None,
            },
        )
        monkeypatch.setattr('carrac.facts.wire.get_asset_facts', lambda s, r=None: None)
        from carrac.mcp.tools.market import market_get_detail

        # ZZSTALE, not FTM (nor KAS, which is curated) — a fictional ticker,
        # because real stand-ins rot. FTM is on instrument_master.LIFECYCLE_OVERRIDES —
        # the migration this docstring names is exactly what a delisting record
        # is for — so it takes the delisted-symbol branch and gets the better
        # answer ('delisted', asserted in tests/test_instrument_lifecycle.py).
        # The no_history path still has to work for everything else.
        out = market_get_detail({'symbol': 'ZZSTALE'})
        assert out['data_stale']['reason'] == 'no_history'
        assert out['price'] == 0.4657, "the live price is still genuinely known"
        assert out['technicals'] is None and out['scanner'] is None and out['facts'] is None

    def test_a_healthy_asset_is_not_swept_up(self, monkeypatch):
        """The failure mode on the other side: flagging everything the moment
        the HTF cache is cold would be just as wrong."""
        monkeypatch.setattr(
            'carrac.composite.assemblers.assemble_asset_detail',
            lambda symbol, user_id=None: {
                'symbol': 'BTC', 'price': 64887.68, 'change_24h': 1.2,
                'technicals': {'consensio': {'1d': 3}, 'rsi_weekly': 54.2},
                'scanner': {'long': {'score': 7}, 'short': {'score': 2}},
            },
        )
        monkeypatch.setattr('carrac.facts.wire.get_asset_facts',
                            lambda s, r=None: {'symbol': 'BTC', 'rsi': {'value': 54.2}})
        from carrac.mcp.tools.market import market_get_detail

        out = market_get_detail({'symbol': 'BTC'})
        assert 'data_stale' not in out
        assert out['technicals'] is not None
        assert out['facts'] is not None


class TestAbsurdPriceMovesAreNotRepublished:
    """FTM carried change_24h 362.06% and change_pct_7d 1525.64% — feed
    artefacts from the token migration, published as fact. One asset today, but
    the number reached the advertised channel with nothing questioning it."""

    def test_an_impossible_daily_move_is_withheld(self):
        from carrac.composite.formatters import compact_asset_detail

        out = compact_asset_detail({'symbol': 'FTM', 'price': 0.4657, 'change_24h': 362.1})
        assert out['change_24h'] is None, "362% in 24h is a feed error, not a move"

    @pytest.mark.parametrize("pct", [0.0, 12.5, -18.4, 63.0, -95.0])
    def test_real_moves_survive(self, pct):
        """A violent but possible day must not be erased. The bar is deliberately
        high — this catches broken feeds, not volatility."""
        from carrac.composite.formatters import compact_asset_detail

        out = compact_asset_detail({'symbol': 'X', 'price': 1.0, 'change_24h': pct})
        assert out['change_24h'] == pct

    def test_missing_change_is_left_alone(self):
        from carrac.composite.formatters import compact_asset_detail
        assert compact_asset_detail({'symbol': 'X', 'price': 1.0})['change_24h'] is None
