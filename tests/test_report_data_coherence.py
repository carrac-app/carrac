"""Pins two report-coherence properties on the FREE PUBLIC /brief page.

Macro facts — the macro-facts strip printed `10Y a% / 2Y b% (spread c)` where
c came from FRED's separately-published T10Y2Y series while a and b came from
the quant `macro` table. Three feeds, three as-of dates, one line: on 8 of 10
sampled briefs the spread could not be derived from the yields beside it, and
it tracked the NEXT trading day's spread 7 times out of 9.

Shared briefing — the shared briefing carried the RS ranking whole, including the
watchlist-only symbols in the RS benchmark set. TSLA reached the public
RS Rankings table in 6 of 10 sampled briefs, ranked as high as #2 of 18.

Both are asserted against the REAL published numbers, not invented fixtures,
so a failure reproduces the exact page a stranger saw.
"""
import pytest

from carrac.grounding.facts_block import _yield_curve
from morning_engine.data_assembly import _registry_rs_only


# (date, 10Y, 2Y, the T10Y2Y spread actually printed that day)
PUBLISHED_BRIEFS = [
    ("2026-07-28", 4.69, 4.33, 0.34), ("2026-07-29", 4.65, 4.31, 0.35),
    ("2026-07-30", 4.61, 4.26, 0.45), ("2026-07-31", 4.67, 4.22, 0.45),
    ("2026-08-01", 4.68, 4.23, 0.47), ("2026-08-02", 4.68, 4.23, 0.47),
    ("2026-08-03", 4.68, 4.23, 0.47), ("2026-08-04", 4.75, 4.28, 0.45),
    ("2026-08-05", 4.70, 4.25, 0.43), ("2026-08-06", 4.63, 4.20, 0.45),
]


# --- Macro facts ----------------------------------------------------------

@pytest.mark.parametrize("date,y10,y2,fred_spread", PUBLISHED_BRIEFS)
def test_spread_closes_on_the_yields_printed_beside_it(date, y10, y2, fred_spread):
    """The subtraction a reader can do on the page must come out right.

    Derived from the ROUNDED yields on purpose: those are the two figures
    actually rendered, so those are the two the arithmetic has to satisfy.
    """
    got_y10, got_y2, spread = _yield_curve({
        "yield_10y_latest": y10,
        "yield_2y_latest": y2,
        "yield_10y_minus_2y": fred_spread,   # the stale-by-a-day feed
    })
    assert (got_y10, got_y2) == (y10, y2)
    assert spread == pytest.approx(round(round(y10, 2) - round(y2, 2), 2), abs=1e-9), (
        f"{date}: printed 10Y {y10:.2f} / 2Y {y2:.2f} but spread {spread:+.2f}"
    )


def test_the_2026_08_06_brief_line_is_now_coherent():
    """The exact line that shipped: 10Y 4.63 / 2Y 4.20 (spread +0.45)."""
    _, _, spread = _yield_curve({
        "yield_10y_latest": 4.63, "yield_2y_latest": 4.20,
        "yield_10y_minus_2y": 0.45,
    })
    assert spread == 0.43, "the published +0.45 was impossible from 4.63 / 4.20"


def test_fred_spread_still_used_when_a_yield_is_missing():
    """Nothing to derive from -> fall back, rather than drop the field."""
    _, y2, spread = _yield_curve({"yield_10y_latest": 4.63,
                                  "yield_10y_minus_2y": 0.45})
    assert y2 is None and spread == 0.45


def test_falls_back_to_yield_10y_pct_when_latest_absent():
    y10, _, _ = _yield_curve({"yield_10y_pct": 4.55, "yield_2y_latest": 4.20})
    assert y10 == 4.55


# --- Shared briefing ----------------------------------------------------------

def test_watchlist_symbol_is_dropped_from_shared_rs():
    """TSLA at rank 3 with +9.43% is a real public-brief row."""
    payload = {"assets": [
        {"symbol": "SPX", "rs_score": 97.2, "ret_7d": 3.97},
        {"symbol": "GOLD", "rs_score": 91.3, "ret_7d": 7.23},
        {"symbol": "TSLA", "rs_score": 91.3, "ret_7d": 9.43},
        {"symbol": "ETH", "rs_score": 85.0, "ret_7d": 2.73},
    ]}
    kept = [r["symbol"] for r in _registry_rs_only(payload)["assets"]]
    assert "TSLA" not in kept
    assert {"SPX", "GOLD", "ETH"} <= set(kept)


def test_registry_symbols_survive_the_filter():
    """The filter must not quietly empty the Relative Strength section."""
    from carrac.assets.registry import chart_assets
    registry = chart_assets()
    assert registry, "registry unexpectedly empty — the rest of this is meaningless"
    payload = {"assets": [{"symbol": s} for s in registry]}
    assert len(_registry_rs_only(payload)["assets"]) == len(registry)


def test_symbol_match_is_case_insensitive():
    payload = {"assets": [{"symbol": "spx"}, {"symbol": "tsla"}]}
    kept = [r["symbol"] for r in _registry_rs_only(payload)["assets"]]
    assert kept == ["spx"]


@pytest.mark.parametrize("payload", [
    None,
    {"status": "error"},
    {"assets": "not-a-list"},
    {"assets": []},
])
def test_degenerate_payloads_pass_through_untouched(payload):
    """A shape change must surface as an unfiltered ranking someone notices,
    never as a silently vanished section."""
    assert _registry_rs_only(payload) == payload


def test_other_keys_are_preserved():
    payload = {"status": "ok", "assets": [{"symbol": "SPX"}, {"symbol": "TSLA"}]}
    out = _registry_rs_only(payload)
    assert out["status"] == "ok"
    assert [r["symbol"] for r in out["assets"]] == ["SPX"]


def test_rows_without_a_symbol_do_not_crash_the_filter():
    payload = {"assets": [{"symbol": "SPX"}, {}, None, {"symbol": None}]}
    assert [r["symbol"] for r in _registry_rs_only(payload)["assets"]] == ["SPX"]


# --- Wiring: the fixes must actually be on the live paths ----------------

def test_shared_context_applies_the_rs_filter():
    """The RS filter is worthless if fetch_context_data() bypasses it."""
    import inspect
    from morning_engine import data_assembly
    src = inspect.getsource(data_assembly.fetch_context_data)
    assert "_registry_rs_only(rs_data)" in src


def test_both_facts_renderers_use_the_shared_yield_helper():
    """Both renderers must use the shared helper; each carrying its own copy of the macro-facts logic drifts."""
    import inspect
    from importlib import import_module
    # NOT `from carrac.grounding import facts_block` — carrac/grounding/__init__.py
    # re-exports the facts_block FUNCTION under that name, shadowing the module.
    fb = import_module("carrac.grounding.facts_block")
    for fn in (fb.facts_block, fb.render_macro_facts_strip):
        src = inspect.getsource(fn)
        assert "_yield_curve(f)" in src, f"{fn.__name__} reads the fields raw"
        assert "yield_10y_minus_2y" not in src, (
            f"{fn.__name__} still reads the FRED spread directly")
