"""Quant signals for the editorial report — pure shaping of a ``build_snapshot()``
result (``quant/api/routes.py``). No I/O. A missing/empty field silently produces
an empty list — never filler, never an error.

Two shapers:
  ``quant_rows(snapshot, symbol)`` — the focus asset's z-score extremes (per-card).
  ``market_anomalies(snapshot)``   — the market-wide ``_GLOBAL_`` z-score extremes
                                     (rates / curve / trends / flows) for the sidebar.

Posture: these are computed statistics (z-scores) — information to evaluate, never
"buy/sell". Lead-lag was dropped from the card (real major→major signal is mostly
contemporaneous/noisy).
"""

from __future__ import annotations

# Friendly labels for the common per-asset z-score metric codes. The asset suffix
# (e.g. ``_BTC``) is stripped before lookup.
_METRIC_LABELS = {
    "DVOL": "Implied volatility",
    "SKEW_25D": "25Δ option skew",
    "FUNDING": "Funding rate",
    "BASIS": "Futures basis",
    "OI": "Open interest",
    "GEX": "Gamma exposure",
    "IV_RV": "IV/RV spread",
    "PUT_CALL": "Put/call ratio",
    "OPTIONS_PC": "Put/call ratio",
    "MVRV": "MVRV",
    "VOL": "Realized volatility",
    "SMART_MONEY_BIAS": "Smart-money bias (Hyperliquid)",
}

# Friendly labels for the market-wide (_GLOBAL_) macro metric codes.
_GLOBAL_METRIC_LABELS = {
    "REVERSE_REPO": "Reverse repo (Fed)",
    "JAPAN_10Y": "Japan 10Y yield",
    "YIELD_10Y2Y": "10Y-2Y curve",
    "YIELD_10Y": "US 10Y yield",
    "YIELD_2Y": "US 2Y yield",
    "STABLECOIN_DOMINANCE": "Stablecoin dominance",
    "GTREND_DEFI": "DeFi search trend",
    "GTREND_BUY_BTC": "'Buy BTC' search trend",
    "GTREND_CRYPTO": "Crypto search trend",
    "MOVE": "MOVE bond vol",
    "VIX": "VIX",
    "HY_SPREAD": "High-yield spread",
}

# Tokens kept upper-cased in the acronym-aware fallback (so ETF_FLOW → "ETF Flow",
# never "Etf flow"). A code that isn't in either label map still reads cleanly.
_ACRONYMS = frozenset({
    "ETF", "BTC", "ETH", "SOL", "XRP", "DXY", "SPX", "QQQ", "COT", "RSI", "MVRV",
    "DVOL", "GEX", "IV", "RV", "POC", "OI", "US", "EU", "FX", "HY", "DEFI", "NFT",
    "GDP", "CPI", "PCE", "PPI", "FOMC", "JPY", "EUR", "USD", "VAH", "VAL", "SMA",
    "EMA", "BB", "ATR", "ADX", "MACD", "TLT", "HYG", "EEM", "MOVE", "VIX",
})


def _clean(code):
    """Acronym-aware sentence-casing of a metric code."""
    out = []
    for tok in (code or "").replace("-", "_").split("_"):
        if not tok:
            continue
        out.append(tok.upper() if tok.upper() in _ACRONYMS else tok.capitalize())
    return " ".join(out)


def _metric_label(metric, symbol):
    suffix = "_" + symbol
    core = metric[:-len(suffix)] if metric.endswith(suffix) else metric
    return _METRIC_LABELS.get(core) or _clean(core)


def _global_label(metric):
    return _GLOBAL_METRIC_LABELS.get(metric) or _clean(metric)


def _shape(rows, label_fn, cap):
    rows = [a for a in rows if a.get("zscore") is not None]
    rows.sort(key=lambda a: abs(a["zscore"]), reverse=True)
    return [{
        "metric": a["metric"],
        "label": label_fn(a["metric"]),
        "zscore": a["zscore"],
        "percentile": a.get("percentile"),
    } for a in rows[:cap]]


def quant_rows(snapshot, symbol, max_extremes=2):
    """The focus asset's z-score extremes (|z| ≥ 2). ``snapshot['anomalies']`` is
    GLOBAL, so filter to ``asset == symbol``; strongest first, capped. Returns a
    list of ``{metric, label, zscore, percentile}`` (possibly empty)."""
    if not snapshot:
        return []
    rows = [a for a in (snapshot.get("anomalies") or []) if a.get("asset") == symbol]
    return _shape(rows, lambda m: _metric_label(m, symbol), max_extremes)


def market_anomalies(snapshot, max_rows=6):
    """The market-wide ``_GLOBAL_`` z-score extremes — where the cross-asset quant
    signal actually lives (rates, curve, search trends, stablecoin flows). Strongest
    first, capped. Returns a list of ``{metric, label, zscore, percentile}``."""
    if not snapshot:
        return []
    rows = [a for a in (snapshot.get("anomalies") or []) if a.get("asset") == "_GLOBAL_"]
    return _shape(rows, _global_label, max_rows)
