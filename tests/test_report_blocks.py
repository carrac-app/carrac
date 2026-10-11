"""Tests for morning_engine.report_blocks (G1 / G2a / G2b).

Renderers are tested via injected stub fetchers — no live API calls.
"""

from morning_engine.report_blocks import (
    build_cot_block,
    build_actuals_block,
    build_anomaly_pulse_block,
    build_sentiment_vol_block,
    build_cross_asset_heat_block,
    build_correlation_shift_block,
    _prettify_metric,
)

BANNED_WORDS = ("buy", "sell", "should", "enter", "target")

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

_COT_PAYLOAD = {
    'data_date': '2026-06-20',
    'stretched_count': 2,
    'series': [
        # 2 stretched rows (should appear first)
        {
            'market_label': 'Gold',
            'type_label': 'Non-Commercials',
            'zscore': 2.8,
            'percentile': 94,
            'wow_change': 12500,
            'stretched': True,
        },
        {
            'market_label': 'S&P 500 E-Mini',
            'type_label': 'Non-Commercials',
            'zscore': 2.3,
            'percentile': 91,
            'wow_change': -5000,
            'stretched': True,
        },
        # 5 non-stretched (sorted by |z| desc; only top 4 shown to stay ≤ 6)
        {
            'market_label': 'WTI Crude',
            'type_label': 'Non-Commercials',
            'zscore': 1.9,
            'percentile': 80,
            'wow_change': 3200,
            'stretched': False,
        },
        {
            'market_label': 'EUR/USD',
            'type_label': 'Non-Commercials',
            'zscore': -1.7,
            'percentile': 15,
            'wow_change': -1800,
            'stretched': False,
        },
        # Copper: wow_change=None → WoW segment must be omitted
        {
            'market_label': 'Copper',
            'type_label': 'Speculator',
            'zscore': 1.4,
            'percentile': 70,
            'wow_change': None,
            'stretched': False,
        },
        {
            'market_label': 'Soybeans',
            'type_label': 'Speculator',
            'zscore': 1.1,
            'percentile': 65,
            'wow_change': 900,
            'stretched': False,
        },
        # This row is 7th and should be truncated (cap = 6)
        {
            'market_label': 'T-Bonds',
            'type_label': 'Non-Commercials',
            'zscore': 0.9,
            'percentile': 60,
            'wow_change': -600,
            'stretched': False,
        },
    ],
}


_EVENTS_MIXED = [
    {
        'title': 'CPI Release (May)',
        'event_type': 'CPI',
        'impact': 'high',
        'expected': None,
        'actual': 'CPI +0.2% MoM / +2.7% YoY',
    },
    {
        'title': 'Non-Farm Payrolls (May)',
        'event_type': 'NFP',
        'impact': 'high',
        'expected': None,
        'actual': 'NFP +185K',
    },
    # No actual — must be excluded from the table
    {
        'title': 'ECB Rate Decision',
        'event_type': 'ECB',
        'impact': 'medium',
        'expected': None,
        'actual': None,
    },
]

_EVENTS_ALL_NONE = [
    {
        'title': 'FOMC Rate Decision',
        'event_type': 'FOMC',
        'impact': 'high',
        'expected': None,
        'actual': None,
    },
]

_ANOMALIES_PAYLOAD = {
    'anomalies': [
        # 6 anomalies — cap at 5 (real-shaped metric names: asset in the metric name)
        {'metric': 'OI_BTC',                 'asset': 'BTC',        'zscore': 3.2,  'percentile': 97.0, 'domain': 'derivatives', 'value': 15000,  'date': '2026-06-26'},
        {'metric': 'FUNDING_ETH',            'asset': 'ETH',        'zscore': -2.8, 'percentile': 3.0,  'domain': 'derivatives', 'value': -0.05,  'date': '2026-06-26'},
        {'metric': 'LS_RATIO_XRP',           'asset': 'XRP',        'zscore': 2.4,  'percentile': 91.0, 'domain': 'derivatives', 'value': 1.5,    'date': '2026-06-26'},
        {'metric': 'REVERSE_REPO',           'asset': '_GLOBAL_',   'zscore': 2.1,  'percentile': 88.0, 'domain': 'macro',       'value': 2200.0, 'date': '2026-06-26'},
        {'metric': 'PC_RATIO_SPX',           'asset': 'SPX',        'zscore': -2.0, 'percentile': 5.0,  'domain': 'macro',       'value': 0.6,    'date': '2026-06-26'},
        # 6th row — should be dropped by the cap
        {'metric': 'IV_BTC',                 'asset': 'BTC',        'zscore': 2.0,  'percentile': 89.0, 'domain': 'spot',        'value': 65.0,   'date': '2026-06-26'},
    ],
}

# Sentiment/vol block stubs
_SENTIMENT_PAYLOAD = {
    'status': 'ok',
    'timestamp': '2026-06-27T06:00:00',
    'sentiment': {
        'fear_greed': {'value': 72, 'classification': 'Greed', 'color': '#f0c040'},
        'long_short': {
            'BTC': {'ratio': 1.23, 'longPct': 55.1, 'shortPct': 44.9, 'color': '#4caf50'},
            'ETH': {'ratio': 0.98, 'longPct': 49.5, 'shortPct': 50.5, 'color': '#f44336'},
            'SOL': {'ratio': 1.05, 'longPct': 51.2, 'shortPct': 48.8, 'color': '#4caf50'},
        },
        'mvrv_zscore': {'zscore': 1.75, 'zone': 'warm', 'color': '#f0c040'},
        'funding_rate': {'rate': 0.0001, 'rate_pct': 0.01, 'color': '#4caf50'},
    },
}

_SNAPSHOT_WITH_BOND_VOL = {
    'anomalies': [],
    'bond_vol': {'level': 115.4, 'label': 'elevated'},
}


# ──────────────────────────────────────────────────────────────────────────────
# G1: build_cot_block
# ──────────────────────────────────────────────────────────────────────────────

def _cot_stub(path, **kw):
    return _COT_PAYLOAD


def test_cot_block_heading():
    result = build_cot_block(api=_cot_stub)
    assert "## COT Positioning" in result
    assert "2026-06-20" in result
    assert "2 stretched" in result


def test_cot_block_stretched_rows_first():
    result = build_cot_block(api=_cot_stub)
    # Gold and S&P 500 E-Mini are stretched — both must appear before WTI Crude
    assert result.index("Gold") < result.index("WTI Crude")
    assert result.index("S&P 500 E-Mini") < result.index("WTI Crude")


def test_cot_block_stretched_marker():
    result = build_cot_block(api=_cot_stub)
    gold_line = next(l for l in result.splitlines() if "Gold" in l)
    sp_line = next(l for l in result.splitlines() if "S&P 500 E-Mini" in l)
    assert "⚠" in gold_line
    assert "⚠" in sp_line


def test_cot_block_non_stretched_has_no_marker():
    result = build_cot_block(api=_cot_stub)
    crude_line = next(l for l in result.splitlines() if "WTI Crude" in l)
    assert "⚠" not in crude_line


def test_cot_block_capped_at_6():
    result = build_cot_block(api=_cot_stub)
    # T-Bonds is the 7th row — must be absent
    assert "T-Bonds" not in result
    # Count data lines (all contain " · ")
    data_lines = [l for l in result.splitlines() if " · " in l]
    assert len(data_lines) <= 6


def test_cot_block_wow_none_omitted():
    result = build_cot_block(api=_cot_stub)
    copper_line = next(l for l in result.splitlines() if "Copper" in l)
    assert "WoW" not in copper_line


def test_cot_block_empty_series_returns_empty():
    stub = lambda path, **kw: {'data_date': '2026-06-20', 'stretched_count': 0, 'series': []}
    assert build_cot_block(api=stub) == ""


def test_cot_block_no_data_returns_empty():
    stub = lambda path, **kw: {}
    assert build_cot_block(api=stub) == ""


def test_cot_block_no_advice_words():
    result = build_cot_block(api=_cot_stub).lower()
    for word in BANNED_WORDS:
        assert word not in result, f"Banned word '{word}' found in COT block"


# ──────────────────────────────────────────────────────────────────────────────
# G2a: build_actuals_block
# ──────────────────────────────────────────────────────────────────────────────

def test_actuals_block_heading():
    result = build_actuals_block(_EVENTS_MIXED)
    assert "## Data Released Today" in result


def test_actuals_block_includes_only_filled():
    result = build_actuals_block(_EVENTS_MIXED)
    assert "CPI Release (May)" in result
    assert "Non-Farm Payrolls (May)" in result
    # ECB has no actual — must be excluded
    assert "ECB Rate Decision" not in result


def test_actuals_block_verbatim_actual():
    result = build_actuals_block(_EVENTS_MIXED)
    assert "CPI +0.2% MoM / +2.7% YoY" in result
    assert "NFP +185K" in result


def test_actuals_block_all_none_returns_empty():
    assert build_actuals_block(_EVENTS_ALL_NONE) == ""


def test_actuals_block_empty_list_returns_empty():
    assert build_actuals_block([]) == ""


def test_actuals_block_markdown_table_structure():
    result = build_actuals_block(_EVENTS_MIXED)
    assert "| Event |" in result
    assert "| Impact |" in result
    assert "| Actual |" in result


def test_actuals_block_no_advice_words():
    result = build_actuals_block(_EVENTS_MIXED).lower()
    for word in BANNED_WORDS:
        assert word not in result, f"Banned word '{word}' found in actuals block"


# ──────────────────────────────────────────────────────────────────────────────
# G2b: build_anomaly_pulse_block
# ──────────────────────────────────────────────────────────────────────────────

def _anomaly_stub(path, **kw):
    return _ANOMALIES_PAYLOAD


def test_anomaly_pulse_heading():
    result = build_anomaly_pulse_block(api=_anomaly_stub)
    assert "## Active Extremes (|z| ≥ 2)" in result


def test_anomaly_pulse_strips_deribit_anomalies_and_refills_slots():
    """The block calls /api/quant/snapshot as the ENGINE (admin),
    so the API's per-caller gate is a no-op — the builder must strip
    unconditionally, like data_assembly's briefing consumer does. Otherwise
    "ETH Skew 25D -3.4σ" and "ETH Basis +3.4σ" get baked into the stored
    afternoon artifact, and the render scrub then deletes the WHOLE line,
    legitimate extremes included."""
    payload = {'anomalies': [
        {'metric': 'SKEW_25D_ETH', 'asset': 'ETH', 'zscore': -3.4,
         'percentile': 4.0, 'domain': 'derivatives_advanced', 'date': '2026-08-26'},
        {'metric': 'BASIS_ETH', 'asset': 'ETH', 'zscore': 3.4,
         'percentile': 99.0, 'domain': 'derivatives_advanced', 'date': '2026-08-26'},
        {'metric': 'FEAR_GREED', 'asset': '_GLOBAL_', 'zscore': 6.7,
         'percentile': 94.0, 'domain': 'sentiment', 'date': '2026-08-26'},
        {'metric': 'JAPAN_10Y', 'asset': '_GLOBAL_', 'zscore': 5.6,
         'percentile': 100.0, 'domain': 'macro', 'date': '2026-08-26'},
        {'metric': 'STABLECOIN_DOMINANCE', 'asset': '_GLOBAL_', 'zscore': -5.0,
         'percentile': 1.0, 'domain': 'sentiment', 'date': '2026-08-26'},
        {'metric': 'FUNDING_ETH', 'asset': 'ETH', 'zscore': 2.6,
         'percentile': 92.0, 'domain': 'derivatives_funding', 'date': '2026-08-26'},
        {'metric': 'LS_RATIO_XRP', 'asset': 'XRP', 'zscore': 2.4,
         'percentile': 91.0, 'domain': 'derivatives_ls', 'date': '2026-08-26'},
    ]}
    result = build_anomaly_pulse_block(api=lambda path, **kw: payload)
    assert 'Skew' not in result and 'Basis' not in result
    # the freed slots go to permitted entries — all five survive the cap
    for label in ('Fear Greed', 'Japan 10Y', 'Stablecoin Dominance',
                  'ETH Funding', 'XRP L/S Ratio'):
        assert label in result, f"permitted extreme {label!r} lost its slot"


def test_anomaly_pulse_sorted_by_abs_zscore():
    result = build_anomaly_pulse_block(api=_anomaly_stub)
    # BTC OI has |z|=3.2 (highest) — must appear before ETH Funding (2.8)
    assert result.index("BTC OI") < result.index("ETH Funding")


def test_anomaly_pulse_capped_at_5():
    result = build_anomaly_pulse_block(api=_anomaly_stub)
    # 6 anomalies input; σ character appears once per anomaly (all on one line)
    assert result.count("σ") <= 5
    # The 6th item (BTC Volume, |z|=2.0, tied with SPX Put Call Ratio)
    # — whichever is 6th should be dropped; we have exactly 5 after capping
    assert result.count("σ") == 5


def test_anomaly_pulse_empty_returns_empty():
    stub = lambda path, **kw: {'anomalies': []}
    assert build_anomaly_pulse_block(api=stub) == ""


def test_anomaly_pulse_no_data_returns_empty():
    stub = lambda path, **kw: {}
    assert build_anomaly_pulse_block(api=stub) == ""


def test_anomaly_pulse_global_asset_not_in_label():
    result = build_anomaly_pulse_block(api=_anomaly_stub)
    # _GLOBAL_ should be stripped from metric labels
    assert "_GLOBAL_" not in result


def test_anomaly_pulse_percentile_shown():
    result = build_anomaly_pulse_block(api=_anomaly_stub)
    # At least one percentile value should appear (e.g. "p97")
    assert "p97" in result or "(p" in result


def test_anomaly_pulse_no_advice_words():
    result = build_anomaly_pulse_block(api=_anomaly_stub).lower()
    for word in BANNED_WORDS:
        assert word not in result, f"Banned word '{word}' found in anomaly pulse"


# --- Anomalies must carry a date, and a staleness marker past 7d

def test_anomaly_pulse_shows_date_and_staleness_marker():
    """A 10-day-stale anomaly (mirrors the real JAPAN_10Y case, 21-23d stale)
    must be dated AND flagged old — the header says 'Active', so the date is
    the only thing telling a reader (or the LLM) it is not current."""
    from datetime import datetime, timedelta, timezone
    stale_date = (datetime.now(timezone.utc) - timedelta(days=10)).strftime('%Y-%m-%d')
    payload = {'anomalies': [
        {'metric': 'JAPAN_10Y', 'asset': '_GLOBAL_', 'zscore': 5.61, 'percentile': 100.0,
         'domain': 'macro', 'value': 1.2, 'date': stale_date},
    ]}
    result = build_anomaly_pulse_block(api=lambda path, **kw: payload)
    assert stale_date in result, result
    assert "old)" in result, result


def test_anomaly_pulse_fresh_date_has_no_staleness_marker():
    from datetime import datetime, timezone
    fresh_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    payload = {'anomalies': [
        {'metric': 'VIX', 'asset': '_GLOBAL_', 'zscore': 2.5, 'percentile': 90.0,
         'domain': 'macro', 'value': 20, 'date': fresh_date},
    ]}
    result = build_anomaly_pulse_block(api=lambda path, **kw: payload)
    assert fresh_date in result, result
    assert "old)" not in result, result


def test_anomaly_pulse_metric_asset_dedup():
    """Verify metric + asset rendering has no duplicated asset and correct abbreviations."""
    result = build_anomaly_pulse_block(api=_anomaly_stub)
    # LS_RATIO_XRP should render as "XRP L/S Ratio" (no "Xrp", asset dedup, L/S abbreviation)
    assert "XRP L/S Ratio" in result
    # OI_BTC should render as "BTC OI" (no "Btc")
    assert "BTC OI" in result
    # PC_RATIO_SPX should render as "SPX P/C Ratio" (P/C abbreviation)
    assert "SPX P/C Ratio" in result
    # REVERSE_REPO should render as "Reverse Repo" (no asset prepended, _GLOBAL_ ignored)
    assert "Reverse Repo" in result
    # No bare "Xrp" or "Btc" suffixes (case-insensitive duplication)
    assert " Xrp" not in result
    assert " Btc" not in result
    assert "Ls " not in result  # Should be "L/S", not "Ls"
    # ETH Funding should render cleanly (abbreviation test)
    assert "ETH Funding" in result


# ──────────────────────────────────────────────────────────────────────────────
# API Exception Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_cot_block_api_raises_returns_empty():
    """Verify build_cot_block returns '' when API raises an exception."""
    def raising_api(path, **kw):
        raise ConnectionError("timeout")

    assert build_cot_block(api=raising_api) == ""


def test_anomaly_pulse_api_raises_returns_empty():
    """Verify build_anomaly_pulse_block returns '' when API raises an exception."""
    def raising_api(path, **kw):
        raise ConnectionError("timeout")

    assert build_anomaly_pulse_block(api=raising_api) == ""


# ──────────────────────────────────────────────────────────────────────────────
# WoW Negative-Zero Test
# ──────────────────────────────────────────────────────────────────────────────


def test_cot_block_wow_negative_zero():
    """Verify WoW segment avoids '-0' for tiny fractional values."""
    payload = {
        'data_date': '2026-06-26',
        'stretched_count': 0,
        'series': [
            {
                'market_label': 'Test',
                'type_label': 'Test Type',
                'zscore': 1.0,
                'percentile': 50,
                'wow_change': -0.4,  # Rounds to 0, should not appear as "-0"
                'stretched': False,
            },
        ],
    }

    def stub(path, **kw):
        return payload

    result = build_cot_block(api=stub)
    # Result should contain "WoW +0" or "WoW 0", never "WoW -0"
    assert "WoW -0" not in result
    assert ("WoW +0" in result or "WoW 0" in result)


# ──────────────────────────────────────────────────────────────────────────────
# _prettify_metric: embedded-asset token fix
# ──────────────────────────────────────────────────────────────────────────────

def test_prettify_funding_xrp_okx():
    """FUNDING_XRP_OKX with asset XRP must drop the embedded XRP token."""
    assert _prettify_metric('FUNDING_XRP_OKX', 'XRP') == 'XRP Funding Okx'


def test_prettify_ls_ratio_xrp():
    """LS_RATIO_XRP with asset XRP → trailing strip + L/S abbreviation."""
    assert _prettify_metric('LS_RATIO_XRP', 'XRP') == 'XRP L/S Ratio'


def test_prettify_reverse_repo_global():
    """REVERSE_REPO with _GLOBAL_ → no asset prefix."""
    assert _prettify_metric('REVERSE_REPO', '_GLOBAL_') == 'Reverse Repo'


# ──────────────────────────────────────────────────────────────────────────────
# G1b: build_sentiment_vol_block
# ──────────────────────────────────────────────────────────────────────────────

def _sentiment_vol_stub(path, **kw):
    if path == '/api/market-sentiment':
        return _SENTIMENT_PAYLOAD
    if path == '/api/quant/snapshot':
        return _SNAPSHOT_WITH_BOND_VOL
    return {}


def test_sentiment_vol_block_heading():
    result = build_sentiment_vol_block(api=_sentiment_vol_stub)
    assert result.startswith("## Market Pulse")


def test_sentiment_vol_block_fear_greed():
    result = build_sentiment_vol_block(api=_sentiment_vol_stub)
    assert "Fear & Greed 72" in result
    assert "Greed" in result


def test_sentiment_vol_block_btc_ls():
    result = build_sentiment_vol_block(api=_sentiment_vol_stub)
    assert "BTC L/S 1.23" in result
    assert "45% short" in result  # 44.9 rounds to 45


def test_sentiment_vol_block_mvrv():
    result = build_sentiment_vol_block(api=_sentiment_vol_stub)
    assert "MVRV-z +1.75" in result
    assert "warm" in result


def test_sentiment_vol_block_move():
    result = build_sentiment_vol_block(api=_sentiment_vol_stub)
    assert "MOVE 115" in result
    assert "elevated" in result


def test_sentiment_vol_block_snapshot_raises_omits_move():
    """When /api/quant/snapshot raises, MOVE segment is omitted but rest is present."""
    def partial_stub(path, **kw):
        if path == '/api/market-sentiment':
            return _SENTIMENT_PAYLOAD
        raise ConnectionError("timeout")

    result = build_sentiment_vol_block(api=partial_stub)
    assert "Fear & Greed" in result
    assert "MOVE" not in result


def test_sentiment_vol_block_both_empty_returns_empty():
    """When both endpoints return empty/None data, return ''."""
    stub = lambda path, **kw: {}
    assert build_sentiment_vol_block(api=stub) == ""


def test_sentiment_vol_block_no_advice_words():
    result = build_sentiment_vol_block(api=_sentiment_vol_stub).lower()
    for word in BANNED_WORDS:
        assert word not in result, f"Banned word '{word}' found in Market Pulse block"


# ──────────────────────────────────────────────────────────────────────────────
# Cross-Asset Heat Block
# ──────────────────────────────────────────────────────────────────────────────

_HEAT_PCT_MAP = {
    "BTC": 58, "ETH": 44, "SOL": 18,
    "SPX": 86, "NDX": 72,
    "GOLD": 4, "OIL": 11,
    "DXY": 97, "TLT": 39, "HYG": 55,
}

BANNED_WORDS_HEAT = ("buy", "sell", "should", "recommend", "i would")


def _heat_get_facts_full(sym):
    """Fake get_facts returning range_pctile for all heat assets."""
    if sym in _HEAT_PCT_MAP:
        return {"symbol": sym, "range_pctile": {"pct": _HEAT_PCT_MAP[sym], "window_days": 90}}
    return None


class TestCrossAssetHeatBlock:

    def test_heading(self):
        result = build_cross_asset_heat_block(get_facts=_heat_get_facts_full)
        assert "## Cross-Asset Heat" in result

    def test_all_groups_present(self):
        result = build_cross_asset_heat_block(get_facts=_heat_get_facts_full)
        assert "Crypto:" in result
        assert "Equity:" in result
        assert "Commodity:" in result
        assert "Macro:" in result

    def test_group_order_crypto_first(self):
        result = build_cross_asset_heat_block(get_facts=_heat_get_facts_full)
        assert result.index("Crypto:") < result.index("Equity:")
        assert result.index("Equity:") < result.index("Commodity:")
        assert result.index("Commodity:") < result.index("Macro:")

    def test_pNN_zero_padded_single_digit(self):
        # GOLD=p4 → must appear as p04; OIL=p11 as p11
        result = build_cross_asset_heat_block(get_facts=_heat_get_facts_full)
        assert "p04" in result
        assert "p11" in result
        assert "p18" in result  # SOL=18
        assert "p58" in result  # BTC=58

    def test_not_advice_footer_present(self):
        result = build_cross_asset_heat_block(get_facts=_heat_get_facts_full)
        assert "Not advice" in result

    def test_90_day_range_mentioned(self):
        result = build_cross_asset_heat_block(get_facts=_heat_get_facts_full)
        assert "90-day" in result

    def test_missing_symbol_is_skipped(self):
        # Only BTC and ETH have data; SOL is absent
        partial_map = {"BTC": 58, "ETH": 44}

        def partial_get_facts(sym):
            if sym in partial_map:
                return {"symbol": sym, "range_pctile": {"pct": partial_map[sym], "window_days": 90}}
            return None

        result = build_cross_asset_heat_block(get_facts=partial_get_facts)
        assert "BTC" in result
        assert "ETH" in result
        assert "SOL" not in result

    def test_all_missing_returns_empty_string(self):
        result = build_cross_asset_heat_block(get_facts=lambda sym: None)
        assert result == ""

    def test_group_omitted_when_all_assets_missing(self):
        # Only Crypto assets have data
        crypto_only = {"BTC": 58, "ETH": 44, "SOL": 18}

        def crypto_get_facts(sym):
            if sym in crypto_only:
                return {"range_pctile": {"pct": crypto_only[sym], "window_days": 90}}
            return None

        result = build_cross_asset_heat_block(get_facts=crypto_get_facts)
        assert "Crypto:" in result
        assert "Equity:" not in result
        assert "Commodity:" not in result
        assert "Macro:" not in result

    def test_no_banned_words(self):
        result = build_cross_asset_heat_block(get_facts=_heat_get_facts_full).lower()
        for word in BANNED_WORDS_HEAT:
            assert word not in result, f"Banned word '{word}' found in heat block"

    def test_asset_ordering_within_group(self):
        # BTC should appear before ETH, ETH before SOL
        result = build_cross_asset_heat_block(get_facts=_heat_get_facts_full)
        assert result.index("BTC") < result.index("ETH") < result.index("SOL")


# ──────────────────────────────────────────────────────────────────────────────
# Guard: _HEAT_GROUPS symbols must all be in the known universe
# ──────────────────────────────────────────────────────────────────────────────

# Static known-universe set (derived from data_sources/stooq.py maps + crypto).
# Extend this set if new valid symbols are added to the universe — do NOT add
# typos or aliases like "NASDAQ" (the valid symbol is "NDX").
_KNOWN_UNIVERSE: frozenset[str] = frozenset({
    # Crypto
    "BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "BNB", "DOGE",
    # Equity indices (from stooq index_map)
    "SPX", "NDX", "DJI", "RUT", "VIX", "SPY",
    "DAX", "FTSE", "CAC", "NIKKEI", "HSI",
    # Commodities (from stooq commodity_map)
    "GOLD", "OIL", "SILVER", "COPPER", "NATGAS",
    # FX / Macro (from stooq forex_map)
    "DXY", "EURUSD", "GBPUSD", "USDJPY",
    # Bonds / ETFs (from stooq etf_map)
    "TLT", "HYG", "LQD", "SHY",
    # Yield indices
    "US10Y", "US30Y", "US5Y",
})


def test_heat_groups_symbols_all_in_known_universe():
    """Every symbol in _HEAT_GROUPS must be a real universe symbol.

    A typo like 'NASDAQ' (should be 'NDX') must
    fail this test offline without any database access.
    """
    from morning_engine.report_blocks import _HEAT_GROUPS

    all_heat_symbols = [s for _, syms in _HEAT_GROUPS for s in syms]
    for sym in all_heat_symbols:
        assert sym in _KNOWN_UNIVERSE, (
            f"Symbol '{sym}' in _HEAT_GROUPS is not in the known universe. "
            f"Common mistake: 'NASDAQ' should be 'NDX'. "
            f"Add '{sym}' to _KNOWN_UNIVERSE if it is genuinely new."
        )


# ──────────────────────────────────────────────────────────────────────────────
# G1d: build_correlation_shift_block
# ──────────────────────────────────────────────────────────────────────────────

BANNED_WORDS_CORR = ("buy", "sell", "should", "recommend", "i would")

_SHIFT_ROWS_2 = [
    {
        "a": "BTC",
        "b": "SPX",
        "prior": 0.61,
        "today": 0.08,
        "shift": -0.53,
        "became_significant": True,
    },
    {
        "a": "GOLD",
        "b": "FEDFUNDS",
        "prior": -0.05,
        "today": 0.39,
        "shift": 0.44,
        "became_significant": False,
    },
]

_SHIFT_ROWS_3 = _SHIFT_ROWS_2 + [
    {
        "a": "ETH",
        "b": "DXY",
        "prior": 0.50,
        "today": 0.10,
        "shift": -0.40,
        "became_significant": False,
    },
]


class TestCorrelationShiftBlock:

    def test_heading_present(self):
        result = build_correlation_shift_block(fetch=lambda: _SHIFT_ROWS_2)
        assert "## Correlation Shifts" in result
        assert "60d" in result or "WoW" in result

    def test_pretty_labels_pair_format(self):
        """Pairs render as 'PrettyA–PrettyB' with en-dash (title-cased via _prettify_metric)."""
        result = build_correlation_shift_block(fetch=lambda: _SHIFT_ROWS_2)
        # _prettify_metric("BTC", "") → "Btc", _prettify_metric("SPX", "") → "Spx"
        assert "Btc" in result
        assert "Spx" in result
        # en-dash must appear between the pair labels
        assert "–" in result

    def test_prior_to_today_format(self):
        """Each line contains 'prior → today' format."""
        result = build_correlation_shift_block(fetch=lambda: _SHIFT_ROWS_2)
        assert "0.61 → 0.08" in result
        assert "−0.05 → 0.39" in result or "-0.05 → 0.39" in result

    def test_shift_in_parens_with_sign(self):
        """Shift appears in parens with sign: (−0.53) or (+0.44)."""
        result = build_correlation_shift_block(fetch=lambda: _SHIFT_ROWS_2)
        # negative shift
        assert "−0.53" in result or "(-0.53)" in result or "−0.53" in result
        # positive shift
        assert "+0.44" in result

    def test_became_significant_tag(self):
        """'now significant' tag appears only when became_significant=True."""
        result = build_correlation_shift_block(fetch=lambda: _SHIFT_ROWS_2)
        # _prettify_metric("BTC", "") → "Btc", _prettify_metric("SPX", "") → "Spx"
        lines = result.splitlines()
        btc_line = next(l for l in lines if "Btc" in l and "Spx" in l)
        assert "now significant" in btc_line
        # GOLD–FEDFUNDS became_significant=False → no tag
        gold_line = next(l for l in lines if "Gold" in l)
        assert "now significant" not in gold_line

    def test_not_advice_footer(self):
        """'Not advice.' caption is present."""
        result = build_correlation_shift_block(fetch=lambda: _SHIFT_ROWS_2)
        assert "Not advice." in result

    def test_empty_fetch_returns_empty_string(self):
        """Empty shift list → ''."""
        assert build_correlation_shift_block(fetch=lambda: []) == ""

    def test_three_rows_rendered(self):
        """3 shift dicts → 3 data lines (excluding heading + footer)."""
        result = build_correlation_shift_block(fetch=lambda: _SHIFT_ROWS_3)
        lines = result.splitlines()
        data_lines = [l for l in lines if "→" in l]
        assert len(data_lines) == 3

    def test_no_banned_words(self):
        """No advice-type language in rendered output."""
        result = build_correlation_shift_block(fetch=lambda: _SHIFT_ROWS_2).lower()
        for word in BANNED_WORDS_CORR:
            assert word not in result, f"Banned word '{word}' found in correlation shift block"
