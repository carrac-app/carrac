"""Tests for the shared `_facts_modules.html` partial (used by the editorial
report and /assets pages).

Scenarios:
1. Partial renders editorial markup (fcard + rung) and is facts-only (no advice words).
2. Partial renders side modules (funding row, correlations, COT, pulse, macro) when supplied.
3. Partial renders heat strip when heat data is supplied.
4. Empty facts_list renders the no-data fallback, not an error.
5. RSI divergence dict renders a clean label, never the raw dict repr.
"""


# ---------------------------------------------------------------------------
# Stub bundle — a minimal but realistic facts bundle for BTC
# ---------------------------------------------------------------------------

_BTC_BUNDLE = {
    "symbol": "BTC",
    "price": 65000.0,
    "as_of": "2026-06-26T00:00:00",
    "ma_stack": {
        "state": "bullish",
        "bars_held": 12,
        "cross": None,
    },
    "rsi": {"value": 58.5, "zone": "neutral", "divergence": "none"},
    "volume": {"rvol": 1.35, "signal": "high", "reliable": True},
    "stretch": {"mayer": 1.15, "mayer_pct": 62},
    "levels": {
        "clusters": [
            {
                "price": 67500.0,
                "side": "resistance",
                "members": ["PDH", "R1"],
                "count": 2,
                "dist_pct": 3.8,
            },
            {
                "price": 63000.0,
                "side": "support",
                "members": ["SMA50", "PDL"],
                "count": 2,
                "dist_pct": -3.1,
            },
        ],
        "nearest_support_dist_pct": 3.1,
        "nearest_resistance_dist_pct": 3.8,
        "monthly_cpr": {"position": "above_bc"},
        "key_levels": {"pdh": 67500.0, "pdl": 63000.0},
    },
    "streaks": [
        {"level": "SMA200", "price": 55000.0, "bars": 30, "type": "above"},
    ],
    "decisive_close": [
        {"level": "PDH", "direction": "above", "bars_ago": 2},
    ],
    "candle": {"o": 64800.0, "h": 65500.0, "l": 64200.0, "c": 65000.0, "prev_close": 64100.0},
    "timeframes": {},
}


def _stub_get_asset_facts(sym):
    """Return the BTC bundle for BTC, None for everything else."""
    if sym == "BTC":
        return _BTC_BUNDLE
    return None


def _stub_all_none(sym):
    """Always return None (simulates cold cache / no data)."""
    return None


_SAMPLE_BUNDLE = dict(_BTC_BUNDLE, funding={
    "sign": "positive",
    "streak_days": 5,
    "percentile": 18,
    "window_days": 90,
})


def _stub_price_none_clusters_populated(sym):
    """Return bundle with price=None but clusters populated (edge case fix validation)."""
    if sym == "ETH":
        return {
            "symbol": "ETH",
            "price": None,  # <-- This is the edge case
            "as_of": "2026-06-26T00:00:00",
            "ma_stack": None,
            "rsi": None,
            "volume": None,
            "stretch": None,
            "levels": {
                "clusters": [
                    {
                        "price": 3500.0,
                        "side": "resistance",
                        "members": ["PDH"],
                        "count": 1,
                        "dist_pct": None,
                    },
                    {
                        "price": 3300.0,
                        "side": "support",
                        "members": ["SMA200"],
                        "count": 1,
                        "dist_pct": None,
                    },
                ],
                "nearest_support_dist_pct": None,
                "nearest_resistance_dist_pct": None,
                "monthly_cpr": None,
                "key_levels": {},
            },
            "streaks": None,
            "decisive_close": None,
            "candle": None,
            "timeframes": {},
        }
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _fast_fail_conn():
    """Helper: stub for quant get_conn that raises immediately (no Postgres timeout)."""
    raise RuntimeError("no quant DB in test env — fast fail")


class TestFactsPartial:
    """Tests for the shared _facts_modules.html partial."""

    def test_partial_renders_editorial_markup(self, app):
        """Partial with a bundle renders fcard + levels ladder; no advice words."""
        from flask import render_template
        with app.test_request_context('/'):
            html = render_template(
                'partials/_facts_modules.html',
                facts_list=[_SAMPLE_BUNDLE],
                heat=None,
                correlations=None,
                cot=None,
                pulse=None,
                macro=None,
            )
        assert 'class="fcard"' in html
        assert 'class="rung' in html        # levels ladder rendered
        assert 'class="priceline"' in html  # price now marker
        # No advice words
        for banned in ('buy', 'sell', ' should ', 'target'):
            assert banned not in html.lower(), f"Banned advice word found: {banned!r}"

    def test_partial_renders_funding_row(self, app):
        """Bundle with funding renders the funding memrow."""
        from flask import render_template
        with app.test_request_context('/'):
            html = render_template(
                'partials/_facts_modules.html',
                facts_list=[_SAMPLE_BUNDLE],
                heat=None,
                correlations=None,
                cot=None,
                pulse=None,
                macro=None,
            )
        assert 'Funding' in html
        assert 'positive' in html

    def test_partial_renders_heat_strip(self, app):
        """Heat strip renders when heat data is supplied."""
        from flask import render_template
        _heat = [
            {'label': 'Crypto', 'rows': [
                {'sym': 'BTC', 'pct': 85},
                {'sym': 'ETH', 'pct': 15},
            ]},
        ]
        with app.test_request_context('/'):
            html = render_template(
                'partials/_facts_modules.html',
                facts_list=[_SAMPLE_BUNDLE],
                heat=_heat,
                correlations=None,
                cot=None,
                pulse=None,
                macro=None,
            )
        assert 'class="heat"' in html
        assert 'class="hrow"' in html
        assert 'p85' in html   # hot marker
        assert 'p15' in html   # cold marker
        assert 'hot' in html
        assert 'cold' in html

    def test_partial_no_data_fallback(self, app):
        """Empty facts_list renders the no-data fallback, not an error."""
        from flask import render_template
        with app.test_request_context('/'):
            html = render_template(
                'partials/_facts_modules.html',
                facts_list=[],
                heat=None,
                correlations=None,
                cot=None,
                pulse=None,
                macro=None,
            )
        assert 'No data available' in html
        assert 'class="fcard"' not in html

    def test_partial_renders_correlations_module(self, app):
        """Side module: correlations list renders .mod.corr with pair/delta rows."""
        from flask import render_template
        _corr = [
            {"pair": "BTC / SPX", "was": 0.25, "now": 0.61, "delta": 0.36, "became_significant": True},
            {"pair": "ETH / GOLD", "was": -0.10, "now": -0.45, "delta": -0.35, "became_significant": False},
        ]
        with app.test_request_context('/'):
            html = render_template(
                'partials/_facts_modules.html',
                facts_list=[_SAMPLE_BUNDLE],
                heat=None,
                correlations=_corr,
                cot=None,
                pulse=None,
                macro=None,
            )
        assert 'class="mod corr"' in html
        assert 'BTC / SPX' in html
        assert 'now significant' in html  # became_significant=True
        assert 'ETH / GOLD' in html

    def test_partial_renders_cot_module(self, app):
        """Side module: COT list renders .mod.cot with label/value rows."""
        from flask import render_template
        _cot = [
            {"label": "Gold · Net spec ⚠", "value": "z +2.3 · 94th pct · WoW +1200"},
            {"label": "Oil · Net spec", "value": "z -1.1 · 14th pct"},
        ]
        with app.test_request_context('/'):
            html = render_template(
                'partials/_facts_modules.html',
                facts_list=[_SAMPLE_BUNDLE],
                heat=None,
                correlations=None,
                cot=_cot,
                pulse=None,
                macro=None,
            )
        assert 'class="mod cot"' in html
        assert 'Gold' in html
        assert 'Oil' in html
        assert 'COT positioning' in html

    def test_partial_renders_pulse_module(self, app):
        """Side module: pulse list renders .mod.pulse with label/value rows."""
        from flask import render_template
        _pulse = [
            {"label": "Fear & Greed", "value": "63 (Greed)"},
            {"label": "BTC L/S", "value": "1.23 (45% short)"},
            {"label": "MVRV-z", "value": "+1.45 (neutral)"},
        ]
        with app.test_request_context('/'):
            html = render_template(
                'partials/_facts_modules.html',
                facts_list=[_SAMPLE_BUNDLE],
                heat=None,
                correlations=None,
                cot=None,
                pulse=_pulse,
                macro=None,
            )
        assert 'class="mod pulse"' in html
        assert 'Market pulse' in html
        assert 'Fear &amp; Greed' in html or 'Fear & Greed' in html
        assert '63 (Greed)' in html
        assert 'MVRV-z' in html

    def test_partial_renders_macro_module(self, app):
        """Side module: macro string renders .mod with Macro facts header."""
        from flask import render_template
        _macro = "Fed funds 5.33% · next FOMC in 3d · 10Y 4.28% / 2Y 4.82% · DXY 104.23"
        with app.test_request_context('/'):
            html = render_template(
                'partials/_facts_modules.html',
                facts_list=[_SAMPLE_BUNDLE],
                heat=None,
                correlations=None,
                cot=None,
                pulse=None,
                macro=_macro,
            )
        assert 'Macro facts' in html
        assert 'Fed funds' in html
        assert '5.33%' in html

    def test_rsi_dict_divergence_renders_clean_label(self, app):
        """RSI divergence as a dict must render a clean label, never the raw dict."""
        from flask import render_template
        # Simulate what the facts wire returns: divergence is a dict with bool fields
        _bundle_with_dict_div = dict(_SAMPLE_BUNDLE, rsi={
            "value": 44.2,
            "zone": "neutral",
            "divergence": {
                "bullish_divergence": False,
                "hidden_bullish_divergence": True,
                "bearish_divergence": False,
                "hidden_bearish_divergence": False,
            },
        })
        with app.test_request_context('/'):
            html = render_template(
                'partials/_facts_modules.html',
                facts_list=[_bundle_with_dict_div],
                heat=None,
                correlations=None,
                cot=None,
                pulse=None,
                macro=None,
            )
        # Must never render the raw dict representation
        assert '{' not in html, "Raw dict found in RSI output"
        assert "'hidden_bullish_divergence'" not in html
        assert "True" not in html.replace("true", "")  # no Python True literals
        # Must render the clean label
        assert 'hidden bull div' in html

    def test_rsi_all_false_divergence_omits_label(self, app):
        """RSI divergence dict with all-False fields must produce no label (no 'div' suffix)."""
        from flask import render_template
        _bundle_no_div = dict(_SAMPLE_BUNDLE, rsi={
            "value": 55.0,
            "zone": "neutral",
            "divergence": {
                "bullish_divergence": False,
                "hidden_bullish_divergence": False,
                "bearish_divergence": False,
                "hidden_bearish_divergence": False,
            },
        })
        with app.test_request_context('/'):
            html = render_template(
                'partials/_facts_modules.html',
                facts_list=[_bundle_no_div],
                heat=None,
                correlations=None,
                cot=None,
                pulse=None,
                macro=None,
            )
        # RSI cell must show value and zone but no divergence label
        assert '55.0' in html
        assert 'bull div' not in html
        assert 'bear div' not in html
        assert 'class="sv"' in html, "RSI statgrid sv cell should always render"
        assert '{' not in html.split('class="sv"')[1].split('</div>')[0], "Raw dict found in sv cell"
