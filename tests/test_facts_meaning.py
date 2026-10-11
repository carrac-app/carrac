"""Tests for translate.meaning(metric, bundle) — plain-English reading glosses.

A separate channel from the percentile-driven ``interpret``: ``meaning`` answers
"what does this reading say in human terms" off the actual reading, not its
rarity. Ported from the v3 preview generator's ``meaning()``.
"""

from carrac.facts.translate import meaning


# ─────────────────────────────────────────────── trend ─────────────────────

def test_meaning_trend_full_bull():
    b = {"ma_stack": {"state": "bullish", "bars_held": 12}}
    assert meaning("trend", b) == "price has held above all its moving averages for 12 days"


def test_meaning_trend_partial_bear():
    b = {"ma_stack": {"state": "bearish_partial", "bars_held": 3}}
    assert meaning("trend", b) == "price has held below most of its moving averages for 3 days"


def test_meaning_trend_none_when_insufficient():
    assert meaning("trend", {"ma_stack": {"state": "insufficient"}}) is None
    assert meaning("trend", {}) is None


# ─────────────────────────────────────────────── rsi ───────────────────────

def test_meaning_rsi_overbought():
    assert meaning("rsi", {"rsi": {"value": 74}}) == "overbought — momentum stretched to the upside"


def test_meaning_rsi_oversold():
    assert meaning("rsi", {"rsi": {"value": 22}}) == "oversold — momentum stretched to the downside"


def test_meaning_rsi_balanced():
    assert meaning("rsi", {"rsi": {"value": 50}}) == "balanced momentum"


def test_meaning_rsi_none_without_value():
    assert meaning("rsi", {"rsi": {}}) is None


# ─────────────────────────────────────────────── mayer ─────────────────────

def test_meaning_mayer_below():
    # mayer 0.8 → 20% below
    assert meaning("mayer", {"stretch": {"mayer": 0.8}}) == \
        "trading about 20% below its 200-week average price"


def test_meaning_mayer_above():
    assert meaning("mayer", {"stretch": {"mayer": 1.3}}) == \
        "trading about 30% above its 200-week average price"


def test_meaning_mayer_none():
    assert meaning("mayer", {"stretch": {}}) is None


# ─────────────────────────────────────────────── volume ────────────────────

def test_meaning_volume_heavy():
    assert meaning("volume", {"volume": {"rvol": 1.8}}) == \
        "trading on heavy volume vs its 20-day norm"


def test_meaning_volume_light():
    assert meaning("volume", {"volume": {"rvol": 0.4}}) == \
        "trading on light volume vs its 20-day norm"


def test_meaning_volume_none():
    assert meaning("volume", {"volume": {}}) is None


# ─────────────────────────────────────────────── funding ───────────────────

def test_meaning_funding_negative():
    assert meaning("funding", {"funding": {"sign": "negative"}}) == \
        "shorts are paying longs to hold the trade"


def test_meaning_funding_positive():
    assert meaning("funding", {"funding": {"sign": "positive"}}) == \
        "longs are paying shorts to hold the trade"


def test_meaning_funding_none():
    assert meaning("funding", {"funding": {}}) is None


# ─────────────────────────────────────────────── range ─────────────────────

def test_meaning_range_bottom():
    assert meaning("range", {"range_pctile": {"pct": 5}}) == \
        "sitting near the bottom of its 90-day range"


def test_meaning_range_top():
    assert meaning("range", {"range_pctile": {"pct": 95}}) == \
        "pressing the top of its 90-day range"


def test_meaning_range_mid():
    assert meaning("range", {"range_pctile": {"pct": 50}}) == "mid 90-day range"


def test_meaning_range_none():
    assert meaning("range", {"range_pctile": {}}) is None


# ─────────────────────────────────────────────── unknown ───────────────────

def test_meaning_unknown_metric_is_none():
    assert meaning("nonsense", {"anything": 1}) is None
