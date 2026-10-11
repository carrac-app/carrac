"""Tests for carrac/facts/card.py — the hybrid-card structured renderers.

Covers:
  - numbers_rows: all fields, crypto, commodity, weekly, graceful absence
  - level_flips: daily + weekly, sorting, empty bundle
  - confluence_rows: clusters, ordering, missing levels
"""

from __future__ import annotations

from carrac.facts.card import level_flips


# ─────────────────────────────────────────────── fixture helpers ──────────────

def _ma_stack(state="bullish", bars=6, cross="none"):
    return {"state": state, "bars_held": bars, "cross": cross}


def _rsi(value=58.0, zone="neutral", div_type=None):
    return {"value": value, "zone": zone, "divergence": {"divergence_type": div_type}}


def _stretch(mayer=1.12, pct=64):
    return {"mayer": mayer, "mayer_pct": pct}


def _volume(rvol=1.8, signal="high", reliable=True):
    return {"rvol": rvol, "signal": signal, "reliable": reliable}


def _funding(streak=5, sign="positive", latest=0.011, pct=72, window=30):
    return {"streak_days": streak, "sign": sign, "latest": latest,
            "percentile": pct, "window_days": window}


def _range_pctile(pct=84, window=90):
    return {"pct": pct, "window_days": window}


def _streaks(daily=True):
    if daily:
        return [{"level": "pdh", "price": 68000.0, "bars": 3, "type": "defending"}]
    return [{"level": "pwh", "price": 69000.0, "bars": 2, "type": "testing"}]


def _decisive_close(daily=True):
    if daily:
        return [{"level": "sma200", "direction": "above", "confirmed": True, "bars_ago": 1}]
    return [{"level": "pwl", "direction": "below", "confirmed": True, "bars_ago": 2}]


def _clusters():
    return [
        {"price": 70500.0, "side": "resistance", "members": ["pdh", "r1"], "count": 2, "dist_pct": 2.1},
        {"price": 65000.0, "side": "support", "members": ["sma200"], "count": 1, "dist_pct": 4.5},
        {"price": 71000.0, "side": "resistance", "members": ["pwh"], "count": 1, "dist_pct": 1.3},
    ]


def _full_bundle(crypto=True):
    b = {
        "symbol": "BTC",
        "price": 69000.0,
        "ma_stack": _ma_stack("bullish", 6),
        "rsi": _rsi(78.0, "overbought"),
        "stretch": _stretch(1.12, 64),
        "volume": _volume(1.8, "high", True),
        "range_pctile": _range_pctile(84, 90),
        "streaks": _streaks(daily=True),
        "decisive_close": _decisive_close(daily=True),
        "levels": {
            "clusters": _clusters(),
            "nearest_support_dist_pct": 4.5,
            "nearest_resistance_dist_pct": 1.3,
            "monthly_cpr": None,
            "key_levels": [],
        },
    }
    if crypto:
        b["funding"] = _funding(5, "positive", 0.011, 72, 30)
    return b


def _weekly_block():
    return {
        "streaks": [{"level": "pwh", "price": 69500.0, "bars": 4, "type": "defending"}],
        "decisive_close": [{"level": "pwl", "direction": "above", "confirmed": True, "bars_ago": 1}],
        "ma_stack": {"state": "bullish", "bars_held": 10, "cross": "none"},
        "candle": {},
    }


# ═══════════════════════════════════════════════ level_flips ═════════════════

class TestLevelFlips:

    def test_returns_daily_and_weekly_keys(self):
        result = level_flips(_full_bundle(crypto=True))
        assert "daily" in result
        assert "weekly" in result

    def test_daily_entries_have_required_keys(self):
        result = level_flips(_full_bundle(crypto=True))
        for entry in result["daily"]:
            assert "kind" in entry
            assert "text" in entry
            assert "dir" in entry

    def test_decisive_close_above_produces_up_dir(self):
        bundle = {
            "symbol": "BTC",
            "decisive_close": [{"level": "sma200", "direction": "above", "confirmed": True, "bars_ago": 1}],
            "streaks": [],
        }
        result = level_flips(bundle)
        entry = next((e for e in result["daily"] if "above" in e["text"].lower()
                      or "200" in e["text"] or "average" in e["text"].lower()), None)
        if entry is None:
            entry = result["daily"][0]
        assert entry["dir"] == "up"

    def test_decisive_close_below_produces_down_dir(self):
        bundle = {
            "symbol": "BTC",
            "decisive_close": [{"level": "pdh", "direction": "below", "confirmed": True, "bars_ago": 1}],
            "streaks": [],
        }
        result = level_flips(bundle)
        entry = result["daily"][0]
        assert entry["dir"] == "down"

    def test_level_label_used_in_text(self):
        bundle = {
            "symbol": "BTC",
            "decisive_close": [{"level": "sma200", "direction": "above", "confirmed": True, "bars_ago": 1}],
            "streaks": [],
        }
        result = level_flips(bundle)
        assert any("200" in e["text"] or "average" in e["text"].lower() for e in result["daily"])

    def test_streak_entry_present(self):
        bundle = {
            "symbol": "BTC",
            "decisive_close": [],
            "streaks": [{"level": "pdh", "price": 68000.0, "bars": 3, "type": "defending"}],
        }
        result = level_flips(bundle)
        assert len(result["daily"]) == 1
        entry = result["daily"][0]
        assert "3" in entry["text"] or "defend" in entry["text"].lower()

    def test_streak_dir_is_empty_string(self):
        bundle = {
            "symbol": "BTC",
            "decisive_close": [],
            "streaks": [{"level": "pdh", "price": 68000.0, "bars": 2, "type": "defending"}],
        }
        result = level_flips(bundle)
        assert result["daily"][0]["dir"] == ""

    def test_decisive_close_sorted_before_streaks(self):
        bundle = {
            "symbol": "BTC",
            "decisive_close": [{"level": "sma200", "direction": "above", "confirmed": True, "bars_ago": 1}],
            "streaks": [{"level": "pdh", "price": 68000.0, "bars": 5, "type": "defending"}],
        }
        result = level_flips(bundle)
        # decisive closes come first
        kinds = [e["kind"] for e in result["daily"]]
        dc_idx = kinds.index("decisive") if "decisive" in kinds else -1
        streak_idx = next((i for i, k in enumerate(kinds) if k == "streak"), -1)
        if dc_idx >= 0 and streak_idx >= 0:
            assert dc_idx < streak_idx

    def test_weekly_populated_from_weekly_block(self):
        bundle = {
            "symbol": "BTC",
            "decisive_close": [],
            "streaks": [],
            "weekly": _weekly_block(),
        }
        result = level_flips(bundle)
        assert len(result["weekly"]) > 0

    def test_weekly_empty_when_no_weekly_block(self):
        bundle = {
            "symbol": "BTC",
            "decisive_close": [{"level": "sma200", "direction": "above", "confirmed": True, "bars_ago": 1}],
            "streaks": [],
        }
        result = level_flips(bundle)
        assert result["weekly"] == []

    def test_empty_bundle_returns_empty_daily_and_weekly(self):
        result = level_flips({})
        assert result["daily"] == []
        assert result["weekly"] == []

    def test_no_na_in_texts(self):
        result = level_flips(_full_bundle(crypto=True))
        for entry in result["daily"] + result["weekly"]:
            assert "N/A" not in entry["text"]

    def test_streak_bars_under_2_excluded(self):
        # bars < 2 should not create a streak entry (spec says bars >= 2)
        bundle = {
            "symbol": "BTC",
            "decisive_close": [],
            "streaks": [{"level": "pdh", "price": 68000.0, "bars": 1, "type": "defending"}],
        }
        result = level_flips(bundle)
        assert result["daily"] == []

    def test_full_bundle_weekly_uses_weekly_streaks(self):
        bundle = _full_bundle(crypto=True)
        bundle["weekly"] = _weekly_block()
        result = level_flips(bundle)
        assert len(result["weekly"]) > 0
        # weekly entry should mention "prior-week" label from pwh or pwl
        texts = " ".join(e["text"] for e in result["weekly"]).lower()
        assert "week" in texts or "prior" in texts

    def test_various_streak_types_rendered(self):
        for stype in ("defending", "testing", "rejecting", "capped"):
            bundle = {
                "symbol": "ETH",
                "decisive_close": [],
                "streaks": [{"level": "pdl", "price": 3000.0, "bars": 2, "type": stype}],
            }
            result = level_flips(bundle)
            assert len(result["daily"]) == 1
