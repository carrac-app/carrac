"""Tests for card.state_rows(bundle) — the editorial label-first card rows.

Three channels per row, no double-counting: VALUE (the number) · BAND (how rare,
from rarity_label) · MEANING (plain English, from translate.meaning). Funding is
rendered as sign + percentile band, NOT the ≈0.000% raw rate (port target #6).
Price is intentionally dropped from the spotlight.
"""

from carrac.facts.card import state_rows
from carrac.facts.labels import ordinal
from carrac.facts.translate import meaning, rarity_label


def _by_label(rows):
    return {r["label"]: r for r in rows}


# ─────────────────────────────────────────────── trend ─────────────────────

def test_state_rows_trend():
    rows = state_rows({"ma_stack": {"state": "bullish", "bars_held": 12}})
    r = _by_label(rows)["Trend"]
    assert r["value"] == "Full bull stack · 12d"
    assert r["band"] is None
    assert r["meaning"] == meaning("trend", {"ma_stack": {"state": "bullish", "bars_held": 12}})


def test_state_rows_trend_omitted_when_insufficient():
    assert "Trend" not in _by_label(state_rows({"ma_stack": {"state": "insufficient"}}))


# ─────────────────────────────────────────────── momentum ──────────────────

def test_state_rows_momentum_uses_editorial_label():
    rows = state_rows({"rsi": {"value": 74}})
    r = _by_label(rows)["Momentum"]
    assert r["value"] == "RSI 74"
    assert r["band"] is None
    assert r["meaning"] == "overbought — momentum stretched to the upside"


# ─────────────────────────────────────────────── stretch ───────────────────

def test_state_rows_stretch_has_band():
    b = {"stretch": {"mayer": 0.82, "mayer_pct": 4}}
    r = _by_label(state_rows(b))["Stretch"]
    assert r["value"] == f"0.82× · {ordinal(4)}"
    assert r["band"] == rarity_label(4)
    assert r["meaning"] == meaning("mayer", b)


# ─────────────────────────────────────────────── participation ─────────────

def test_state_rows_participation_requires_reliable():
    r = _by_label(state_rows({"volume": {"rvol": 1.8, "reliable": True}}))["Participation"]
    assert r["value"] == "1.8× median"
    assert r["band"] is None


def test_state_rows_participation_omitted_when_unreliable():
    rows = state_rows({"volume": {"rvol": 1.8, "reliable": False}})
    assert "Participation" not in _by_label(rows)


# ─────────────────────────────────────────────── funding (#6) ──────────────

def test_state_rows_funding_is_sign_and_band_not_raw_rate():
    b = {"funding": {"percentile": 1, "sign": "negative", "latest": 0.0001, "streak_days": 4}}
    r = _by_label(state_rows(b))["Funding"]
    assert r["value"] == f"Negative · {ordinal(1)}"
    assert "%" not in r["value"]            # the raw ≈0.000% rate must NOT appear
    assert r["band"] == rarity_label(1)
    assert r["meaning"] == "shorts are paying longs to hold the trade"


def test_state_rows_funding_positive_sign_word():
    b = {"funding": {"percentile": 88, "sign": "positive"}}
    assert _by_label(state_rows(b))["Funding"]["value"] == f"Positive · {ordinal(88)}"


def test_state_rows_funding_omitted_without_percentile():
    assert "Funding" not in _by_label(state_rows({"funding": {"sign": "negative"}}))


# ─────────────────────────────────────────────── range ─────────────────────

def test_state_rows_range():
    b = {"range_pctile": {"pct": 95}}
    r = _by_label(state_rows(b))["Range"]
    assert r["value"] == f"{ordinal(95)} of 90 days"
    assert r["band"] == rarity_label(95)


# ─────────────────────────────────────────────── ordering / price ──────────

def test_state_rows_order_and_no_price_row():
    b = {
        "ma_stack": {"state": "bullish", "bars_held": 5},
        "rsi": {"value": 60},
        "stretch": {"mayer": 1.1, "mayer_pct": 70},
        "volume": {"rvol": 1.2, "reliable": True},
        "funding": {"percentile": 50, "sign": "positive"},
        "range_pctile": {"pct": 40},
        "price": 12345.0,
    }
    labels = [r["label"] for r in state_rows(b)]
    assert labels == ["Trend", "Momentum", "Stretch", "Participation", "Funding", "Range"]
    assert "Price" not in labels


def test_state_rows_empty_bundle_is_empty():
    assert state_rows({}) == []
