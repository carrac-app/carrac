"""Tests for the editorial confluence-ladder engine (carrac/facts/ladder.py).

The ladder is the spatial key-levels block ported from the retired editorial v3
preview generator. It consumes a
``get_symbol_confluence`` payload (the scanner's enriched zones) and produces
structured render rows — no HTML, no I/O — so the template renders them.
"""

from pytest import approx

from carrac.facts import ladder as L


# ─────────────────────────────────────────── fixtures ──────────────────────

def _member(code, name, weight, price=0.0):
    return {"code": code, "name": name, "weight": weight, "price": price, "family": "x"}


def _zone(price, strength, tier, members, side, count=None):
    return {
        "price": price,
        "strength": strength,
        "tier": tier,
        "members": members,
        "side": side,
        "count": count if count is not None else len(members),
        "label_friendly": " + ".join(m["name"] for m in members),
    }


def _payload(price, supports=None, resistances=None):
    return {
        "symbol": "TEST",
        "price": price,
        "supports": supports or [],
        "resistances": resistances or [],
        "all": (supports or []) + (resistances or []),
    }


# ─────────────────────────────────────────── zone_refs ─────────────────────

def test_zone_refs_orders_by_weight_descending():
    """Heaviest-weight members lead (the magnets), light ones trail."""
    z = _zone(100, 3.0, "faint", [
        _member("PDH", "Prev Day High", 1.0),
        _member("SMA200", "200-day SMA", 3.0),
    ], "support")
    assert L.zone_refs(z) == ["200-Day Average", "Previous Day High"]


def test_zone_refs_applies_editorial_override_names():
    """Editorial spelled-out names override the terser scanner LABEL_NAMES."""
    z = _zone(100, 2.0, "faint", [_member("BB_L", "BB Lower", 2.0)], "support")
    assert L.zone_refs(z) == ["Lower Bollinger Band"]


def test_zone_refs_dedupes_collapsed_names():
    """EMA200 and SMA200 both spell to '200-Day Average' — show it once."""
    z = _zone(100, 4.0, "faint", [
        _member("EMA200", "200 EMA", 3.0),
        _member("SMA200", "200 SMA", 3.0),
    ], "support")
    assert L.zone_refs(z) == ["200-Day Average"]


def test_zone_refs_falls_back_to_member_name_for_unknown_code():
    z = _zone(100, 1.0, "faint", [_member("WAT", "Whatever Level", 1.0)], "support")
    assert L.zone_refs(z) == ["Whatever Level"]


# ─────────────────────────────────────────── pick_zones ────────────────────

def test_pick_zones_takes_nearest_two_then_strongest():
    """Nearest 2 by distance, plus the strongest wall if it's strong and not
    already chosen — so a stacked wall farther out still shows."""
    zones = [
        _zone(101, 2.0, "faint", [], "resistance"),
        _zone(103, 2.0, "faint", [], "resistance"),
        _zone(110, 8.0, "strong", [], "resistance"),
    ]
    picked = L.pick_zones(zones, 100)
    prices = [z["price"] for z in picked]
    # nearest two = 101, 103; strongest (110, s=8) added; sorted high→low
    assert prices == [110, 103, 101]


def test_pick_zones_no_duplicate_when_strongest_already_near():
    zones = [
        _zone(101, 9.0, "strong", [], "resistance"),
        _zone(103, 2.0, "faint", [], "resistance"),
    ]
    picked = L.pick_zones(zones, 100)
    assert [z["price"] for z in picked] == [103, 101]


def test_pick_zones_skips_weak_strongest():
    """A 3rd-nearest wall below the strong threshold is NOT pulled in."""
    zones = [
        _zone(101, 2.0, "faint", [], "resistance"),
        _zone(103, 2.0, "faint", [], "resistance"),
        _zone(110, 4.0, "faint", [], "resistance"),  # strongest but < 5.0
    ]
    picked = L.pick_zones(zones, 100)
    assert [z["price"] for z in picked] == [103, 101]


def test_pick_zones_empty_returns_empty():
    assert L.pick_zones([], 100) == []


# ─────────────────────────────────────────── ladder ────────────────────────

def test_ladder_none_for_missing_payload():
    assert L.ladder(None) is None


def test_ladder_none_without_price():
    assert L.ladder(_payload(0, resistances=[_zone(105, 8, "strong", [], "resistance")])) is None


def test_ladder_builds_structured_zone():
    payload = _payload(100, resistances=[
        _zone(105, 8.0, "strong", [
            _member("SMA200", "200 SMA", 3.0),
            _member("PDH", "Prev Day High", 1.0),
            _member("BB_U", "BB Upper", 2.0),
        ], "resistance", count=3),
    ])
    out = L.ladder(payload)
    assert out["price"] == 100
    assert len(out["resistances"]) == 1
    assert out["supports"] == []
    z = out["resistances"][0]
    assert z["price"] == 105
    assert z["dist_pct"] == 5.0          # +5% above price
    assert z["strength"] == 8.0
    assert z["tier"] == "strong"
    assert z["tier_word"] == "Strong wall"
    assert z["signal_count"] == 3
    assert z["bar_pct"] == 50            # 8/16 * 100
    assert z["side"] == "resistance"
    assert z["refs"][0] == "200-Day Average"  # heaviest weight leads


def test_ladder_support_distance_is_negative():
    payload = _payload(100, supports=[_zone(95, 6.0, "medium", [], "support")])
    z = L.ladder(payload)["supports"][0]
    assert z["dist_pct"] == -5.0


def test_ladder_bar_pct_clamped():
    """Bar fills to 100 for a monster wall, floors at 8 for a sliver."""
    big = L.ladder(_payload(100, resistances=[_zone(105, 40.0, "strong", [], "resistance")]))
    small = L.ladder(_payload(100, resistances=[_zone(105, 0.5, "faint", [], "resistance")]))
    assert big["resistances"][0]["bar_pct"] == 100
    assert small["resistances"][0]["bar_pct"] == 8


# ─────────────────────────────────────────── ladder_for (I/O adapter) ───────

def test_ladder_for_builds_from_scanner_cache(monkeypatch):
    payload = _payload(100, resistances=[_zone(105, 8.0, "strong", [], "resistance")])
    monkeypatch.setattr("carrac.scanner.confluence_query.get_symbol_confluence",
                        lambda sym: (payload, "ok"))
    out = L.ladder_for("BTC")
    assert out is not None
    assert out["price"] == 100
    assert len(out["resistances"]) == 1


def test_ladder_for_none_when_cache_cold(monkeypatch):
    monkeypatch.setattr("carrac.scanner.confluence_query.get_symbol_confluence",
                        lambda sym: (None, "cold"))
    assert L.ladder_for("BTC") is None


def test_ladder_for_none_when_symbol_absent(monkeypatch):
    monkeypatch.setattr("carrac.scanner.confluence_query.get_symbol_confluence",
                        lambda sym: (None, "not_found"))
    assert L.ladder_for("NOPE") is None


# ─────────────────────────────────────────── quiet_wall ────────────────────

def test_quiet_wall_flags_strong_resistance_as_ceiling():
    payload = _payload(100, resistances=[
        _zone(100.9, 8.0, "strong", [_member("POC", "Volume POC", 3.0)], "resistance", count=8),
    ])
    w = L.quiet_wall(payload)
    assert w["side"] == "res"
    assert w["word"] == "ceiling"
    assert w["dist_pct"] == approx(0.9)
    assert w["signal_count"] == 8


def test_quiet_wall_flags_strong_support_as_floor():
    payload = _payload(100, supports=[_zone(99.0, 6.0, "strong", [], "support", count=3)])
    w = L.quiet_wall(payload)
    assert w["side"] == "sup"
    assert w["word"] == "floor"
    assert w["dist_pct"] == approx(-1.0)


def test_quiet_wall_none_when_only_weak_tiers():
    payload = _payload(100, resistances=[_zone(100.5, 9.0, "medium", [], "resistance")])
    assert L.quiet_wall(payload) is None


def test_quiet_wall_none_when_strong_wall_too_far():
    # +10% away → proximity weight 0 → below the 1.0 threshold
    payload = _payload(100, resistances=[_zone(110, 9.0, "strong", [], "resistance")])
    assert L.quiet_wall(payload) is None


def test_quiet_wall_none_when_near_but_too_weak():
    # 0.5% away, strength 1.0 → score 1.0*(1-0.5/6)=0.916 < 1.0
    payload = _payload(100, resistances=[_zone(100.5, 1.0, "strong", [], "resistance")])
    assert L.quiet_wall(payload) is None


def test_quiet_wall_picks_highest_proximity_score():
    payload = _payload(100,
                       resistances=[_zone(100.5, 2.0, "strong", [], "resistance")],   # score ~1.8
                       supports=[_zone(99.7, 8.0, "strong", [], "support", count=5)])  # score ~7.6
    w = L.quiet_wall(payload)
    assert w["side"] == "sup" and w["signal_count"] == 5


def test_quiet_wall_none_for_missing_or_priceless_payload():
    assert L.quiet_wall(None) is None
    assert L.quiet_wall(_payload(0, resistances=[_zone(50, 9, "strong", [], "resistance")])) is None


# ─────────────────────────────────────────── levels_summary ──

class TestLevelsSummary:
    def _lad(self):
        return {"supports": [{"price": 106234.0, "dist_pct": -2.0, "tier": "strong",
                              "tier_word": "Strong wall", "signal_count": 8, "bar_pct": 90}],
                "resistances": [{"price": 112400.0, "dist_pct": 1.4, "tier": "moderate",
                                 "tier_word": "Moderate", "signal_count": 4, "bar_pct": 55}]}

    def test_both_sides(self):
        from carrac.facts.ladder import levels_summary
        s = levels_summary(self._lad())
        assert s["support"]["price"] == 106234.0 and s["support"]["signal_count"] == 8
        assert s["resistance"]["tier_word"] == "Moderate"

    def test_one_side_missing(self):
        from carrac.facts.ladder import levels_summary
        lad = self._lad(); lad["resistances"] = []
        s = levels_summary(lad)
        assert s["resistance"] is None and s["support"] is not None

    def test_empty_returns_none(self):
        from carrac.facts.ladder import levels_summary
        assert levels_summary(None) is None
        assert levels_summary({"supports": [], "resistances": []}) is None
