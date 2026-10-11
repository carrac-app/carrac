from morning_engine.confluence_block import select_confluence_focus, render_confluence_focus
from morning_engine.engine import _build_levels_in_focus


def _payload(price, sups, ress):
    """Helper to build confluence payloads for testing."""
    return {"price": price,
            "supports": [{"price": p, "strength": s, "side": "support"} for p, s in sups],
            "resistances": [{"price": p, "strength": s, "side": "resistance"} for p, s in ress]}, "ok"


# Task 2: select_confluence_focus tests
def test_abstains_on_cold_and_not_found():
    def fetch(sym):
        return (None, {"BTC": "cold", "ETH": "not_found"}[sym])
    assert select_confluence_focus(["BTC", "ETH"], fetch) == []


def test_ranks_by_proximity_and_caps():
    data = {
        "BTC": _payload(100, [(99, "strong")], [(110, "strong")]),   # nearest 1% away
        "ETH": _payload(100, [(80, "mod")], [(101, "mod")]),         # nearest 1% away (res)
        "XRP": _payload(100, [(90, "mod")], [(105, "mod")]),         # nearest 5% away
        "SOL": _payload(100, [(70, "mod")], [(130, "mod")]),         # nearest 30% away
    }
    out = select_confluence_focus(["XRP", "SOL", "BTC", "ETH"], lambda s: data[s], max_assets=3)
    assert [f["asset"] for f in out] == ["BTC", "ETH", "XRP"]  # closest-first, SOL dropped
    assert out[0]["support"]["price"] == 99


# Task 3: render_confluence_focus tests
def test_render_empty_is_blank():
    assert render_confluence_focus([]) == ""


def test_render_table_has_levels_and_strength():
    focus = [{"asset": "BTC", "price": 62890,
              "support": {"price": 62020, "strength": 5.2, "tier": "strong"},
              "resistance": {"price": 66008, "strength": 6.1, "tier": "strong"}, "proximity": 0.014}]
    out = render_confluence_focus(focus)
    assert "Levels in Focus" in out and "REPRODUCE" in out
    assert "62,020" in out and "66,008" in out
    assert "1.4% below" in out and "5.0% above" in out
    assert "strong" in out
    assert "5.2" not in out
    assert "6.1" not in out


# Task 4: _build_levels_in_focus helper in engine.py
def test_build_levels_in_focus_uses_symbol_and_renders():
    calls = []
    def api(path, **kw):
        calls.append(path)
        return {"price": 100, "supports": [{"price": 98, "strength": "strong", "side": "support"}],
                "resistances": [{"price": 105, "strength": "mod", "side": "resistance"}]}
    block, _ = _build_levels_in_focus(["BTC"], api)
    assert "symbol=BTC" in calls[0] and "asset=" not in calls[0]
    assert "Levels in Focus" in block and "98" in block


# Task 5: Prompt instruction test
def test_prompt_has_levels_in_focus_instruction():
    from morning_engine.prompts import free_tier_report_prompt
    p = free_tier_report_prompt("", "")
    assert "Levels in Focus" in p
    assert "reproduce" in p.lower() and "do not invent" in p.lower()
