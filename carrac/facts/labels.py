"""Shared display labels + thresholds for the facts-thick report card.

Centralises (1) friendly asset names, (2) named-level labels, and (3) the band
thresholds, so the sentence library (`sentences.py`) and the card renderers
(`card.py`) agree on one vocabulary. Pure data + two lookup helpers; no I/O.
"""

from __future__ import annotations

# Friendly names for the curated universe (+ common extras). Fallback = ticker.
_ASSET_NAMES: dict[str, str] = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP",
    "TOTAL": "Total Crypto Cap",
    "SPX": "S&P 500", "QQQ": "Nasdaq 100", "NDX": "Nasdaq 100", "MAGS": "Magnificent 7",
    "GOLD": "Gold", "SILVER": "Silver", "OIL": "Crude oil", "COPPER": "Copper",
    "NATGAS": "Natural gas", "CORN": "Corn", "WHEAT": "Wheat", "SOYBEANS": "Soybeans",
    "XLE": "Energy (XLE)",
    "DXY": "Dollar index", "TLT": "20Y Treasuries", "HYG": "High-yield credit",
    "GOLD_QQQ": "Gold / Nasdaq", "QQQ_SPX": "Nasdaq / S&P", "MAGS_SPX": "Mag-7 / S&P",
}

# Named-level codes (key_levels + monthly CPR + MA levels) → human labels.
# Fallback = the code upper-cased.
_LEVEL_LABELS: dict[str, str] = {
    "pdh": "prior-day high", "pdl": "prior-day low",
    "pwh": "prior-week high", "pwl": "prior-week low",
    "pmh": "prior-month high", "pml": "prior-month low",
    "mh": "Monday high", "ml": "Monday low",
    "sma20": "20-day average", "sma50": "50-day average", "sma200": "200-day average",
    "pivot": "monthly pivot", "tc": "monthly CPR top", "bc": "monthly CPR bottom",
    "r1": "monthly R1", "r2": "monthly R2", "r3": "monthly R3",
    "s1": "monthly S1", "s2": "monthly S2", "s3": "monthly S3",
}

# Band thresholds — one source of truth for "stretched", "extreme", etc.
STRETCHED_PCT = 90      # range / mayer percentile at/above this = stretched
DEPRESSED_PCT = 10      # at/below this = depressed


def asset_name(symbol: str) -> str:
    """Friendly name for a ticker; falls back to the ticker itself."""
    return _ASSET_NAMES.get(symbol, symbol)


def level_label(code: str) -> str:
    """Human label for a named-level code; falls back to the upper-cased code."""
    if not code:
        return ""
    return _LEVEL_LABELS.get(code.lower(), code.upper())


def ordinal(n: int) -> str:
    """Format an integer as an ordinal string (e.g. 92 → '92nd', 11 → '11th')."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"
