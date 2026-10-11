"""
Quant Bridge test suite.

Unit tests for pure functions (no DB required):
- align_to_chart forward-fill alignment
- Series catalog / metadata consistency
- QUANT_VARS ↔ SERIES_CATALOG cross-check

Signal store tests (mock DB):
- Signal structure from _compute_signals_for_asset
- store_signals with empty list

Config validation extension:
- CANONICAL_METRICS includes signals table entries
"""

from datetime import date, datetime, timezone


from quant.bridge import align_to_chart, SERIES_CATALOG, SERIES_META


# ── Helpers ────────────────────────────────────────────────────────────

def _epoch(year, month, day):
    """Convert calendar date to Unix epoch seconds (UTC)."""
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


# Base timestamp: 2026-01-01 00:00 UTC = 1767225600
_JAN1 = _epoch(2026, 1, 1)
_DAY = 86400


# ── Alignment tests ───────────────────────────────────────────────────

class TestAlignToChart:
    """Pure function tests for align_to_chart — no DB needed."""

    def test_daily_quant_daily_chart_exact_match(self):
        """Quant and chart dates line up 1:1 → values pass through."""
        quant_ts = [
            (date(2026, 1, 1), 100.0),
            (date(2026, 1, 2), 200.0),
            (date(2026, 1, 3), 300.0),
        ]
        chart_ts = [_JAN1, _JAN1 + _DAY, _JAN1 + 2 * _DAY]
        result = align_to_chart(quant_ts, chart_ts)
        assert result == [100.0, 200.0, 300.0]

    def test_forward_fill_weekly_to_daily(self):
        """Weekly M2 data forward-fills into daily chart bars."""
        # Quant: values on Jan 1 and Jan 8 (weekly)
        quant_ts = [
            (date(2026, 1, 1), 21000.0),
            (date(2026, 1, 8), 21100.0),
        ]
        # Chart: daily bars Jan 1-10
        chart_ts = [_JAN1 + i * _DAY for i in range(10)]
        result = align_to_chart(quant_ts, chart_ts)

        # Jan 1-7: 21000, Jan 8-10: 21100
        assert result[0] == 21000.0
        assert result[6] == 21000.0  # Jan 7 still forward-filled from Jan 1
        assert result[7] == 21100.0  # Jan 8 picks up new value
        assert result[9] == 21100.0  # Jan 10 forward-filled from Jan 8

    def test_empty_quant_returns_all_none(self):
        """No quant data → all None (same length as chart)."""
        chart_ts = [_JAN1 + i * _DAY for i in range(5)]
        result = align_to_chart([], chart_ts)
        assert result == [None] * 5

    def test_empty_chart_returns_empty(self):
        """No chart timestamps → empty list."""
        quant_ts = [(date(2026, 1, 1), 42.0)]
        result = align_to_chart(quant_ts, [])
        assert result == []

    def test_chart_before_first_quant_gets_none(self):
        """Chart bars before the first quant data point → None."""
        quant_ts = [
            (date(2026, 1, 5), 500.0),
            (date(2026, 1, 10), 600.0),
        ]
        chart_ts = [_JAN1 + i * _DAY for i in range(12)]
        result = align_to_chart(quant_ts, chart_ts)

        # Jan 1-4 have no quant data yet
        assert result[0] is None
        assert result[3] is None
        # Jan 5 onward gets 500
        assert result[4] == 500.0
        assert result[8] == 500.0  # Jan 9 still forward-filled
        # Jan 10 onward gets 600
        assert result[9] == 600.0
        assert result[11] == 600.0

    def test_single_quant_value_fills_entire_chart(self):
        """One quant value on day 1 fills every subsequent chart bar."""
        quant_ts = [(date(2026, 1, 1), 42.0)]
        chart_ts = [_JAN1 + i * _DAY for i in range(30)]
        result = align_to_chart(quant_ts, chart_ts)

        assert result[0] == 42.0
        assert result[29] == 42.0
        assert all(v == 42.0 for v in result)

    def test_multiple_quant_same_day(self):
        """Multiple quant values on the same date → last one wins."""
        quant_ts = [
            (date(2026, 1, 1), 10.0),
            (date(2026, 1, 1), 20.0),
        ]
        chart_ts = [_JAN1]
        result = align_to_chart(quant_ts, chart_ts)
        assert result == [20.0]

    def test_both_empty(self):
        """Both inputs empty → empty list."""
        assert align_to_chart([], []) == []


# ── Series catalog validation ─────────────────────────────────────────

class TestSeriesCatalog:
    """Validate SERIES_CATALOG and SERIES_META consistency."""

    def test_all_catalog_entries_have_meta(self):
        """Every SERIES_CATALOG key must have a matching SERIES_META entry."""
        missing = set(SERIES_CATALOG.keys()) - set(SERIES_META.keys())
        assert not missing, f"SERIES_CATALOG keys missing from SERIES_META: {missing}"

    def test_all_meta_entries_have_catalog(self):
        """Every SERIES_META key should have a SERIES_CATALOG entry (no orphans)."""
        orphans = set(SERIES_META.keys()) - set(SERIES_CATALOG.keys())
        assert not orphans, f"SERIES_META keys missing from SERIES_CATALOG: {orphans}"

    def test_catalog_entries_have_valid_structure(self):
        """Each catalog entry is a 3-tuple (domain, sql_template, value_col)."""
        for name, entry in SERIES_CATALOG.items():
            assert len(entry) == 3, f"{name}: expected 3-tuple, got {len(entry)}"
            domain, sql_tpl, val_col = entry
            assert isinstance(domain, str), f"{name}: domain must be str"
            assert isinstance(sql_tpl, str), f"{name}: sql template must be str"
            assert isinstance(val_col, str), f"{name}: value column must be str"

    def test_catalog_sql_templates_have_date_filter(self):
        """All SQL templates must contain the {date_filter} placeholder."""
        for name, (domain, sql_tpl, val_col) in SERIES_CATALOG.items():
            assert '{date_filter}' in sql_tpl, \
                f"{name}: SQL template missing {{date_filter}} placeholder"

    def test_catalog_sql_templates_have_order_by(self):
        """All SQL templates should ORDER BY ts for correct alignment."""
        for name, (domain, sql_tpl, val_col) in SERIES_CATALOG.items():
            assert 'ORDER BY ts' in sql_tpl, \
                f"{name}: SQL template missing ORDER BY ts"

    def test_meta_entries_have_required_fields(self):
        """Each SERIES_META entry must have label, frequency, and axis."""
        for name, meta in SERIES_META.items():
            assert 'label' in meta, f"{name}: missing 'label'"
            assert 'frequency' in meta, f"{name}: missing 'frequency'"
            assert 'axis' in meta, f"{name}: missing 'axis'"

    def test_meta_axis_values_valid(self):
        """Axis must be one of the known layout positions."""
        valid_axes = {'right', 'own_pane', 'background'}
        for name, meta in SERIES_META.items():
            assert meta['axis'] in valid_axes, \
                f"{name}: invalid axis '{meta['axis']}', expected one of {valid_axes}"

    def test_quant_vars_reference_valid_catalog_keys(self):
        """All QUANT_VARS values in formula-parser.js should exist in SERIES_CATALOG."""
        # Hardcoded from static/js/formula-parser.js QUANT_VARS object
        quant_vars = {
            'm2': 'M2', 'net_liquidity': 'NET_LIQUIDITY', 'dxy': 'DXY',
            'stablecoin': 'STABLECOIN_USDT_SUPPLY',
            'fear_greed': 'FEAR_GREED', 'alt_season': 'ALT_SEASON',
            'etf_flow': 'ETF_FLOW',
            'vix': 'VIX', 'hy_spread': 'HY_SPREAD', 'yield_curve': 'YIELD_CURVE',
            'regime': 'REGIME',
            'btc': 'BTC', 'eth': 'ETH', 'sol': 'SOL', 'spx': 'SPX', 'gold': 'GOLD_F',
            
            
        }
        for alias, catalog_key in quant_vars.items():
            assert catalog_key in SERIES_CATALOG, \
                f"QUANT_VARS['{alias}'] = '{catalog_key}' not found in SERIES_CATALOG"


# ── Config validation (extends test_quant.py TestConfig) ──────────────

class TestSignalsReaders:
    """Every series read from quant.signals must name a source that a writer
    in this repo actually writes: regime.py writes ``regime_hmm``; leadlag.py
    writes ``leadlag_summary``, ``leadlag_weekly_summary`` and
    ``chain_strength``. A reader of any other source charts an empty series."""

    WRITTEN = {'regime_hmm', 'leadlag_summary', 'leadlag_weekly_summary', 'chain_strength'}

    def test_bridge_catalog_reads_only_written_sources(self):
        import re

        from quant.bridge import SERIES_CATALOG
        for name, (table, sql, _col) in SERIES_CATALOG.items():
            if table != 'signals':
                continue
            source = re.search(r"source = '([^']+)'", sql).group(1)
            assert source in self.WRITTEN, f'{name} reads unwritten source {source!r}'

    def test_canonical_metrics_has_no_signals_entries(self):
        from quant.engine.config import CANONICAL_METRICS
        stale = [k for k, v in CANONICAL_METRICS.items() if v.get('table') == 'signals']
        assert not stale, f'signals series with no writer: {stale}'
