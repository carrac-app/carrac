from carrac.grounding.facts_block import render_macro_facts_strip


def test_macro_strip_renders_present_fields():
    facts = {
        "today": "2026-06-26",
        "fed_funds_latest": 4.33,
        "next_fomc": "2026-07-18",          # 22 days after today
        "yield_10y_latest": 4.21,
        "yield_2y_latest": 3.88,
        "yield_10y_minus_2y": 0.33,
        "dxy_spot": 99.4,
        "cpi_yoy": 2.6,
        "btc_usd": 61240,
        "spx": 5470,
    }
    out = render_macro_facts_strip(facts)
    assert "Fed funds 4.33%" in out
    assert "next FOMC in 22d" in out
    assert "10Y 4.21% / 2Y 3.88%" in out
    assert "DXY 99.40" in out
    assert "BTC $61,240" in out
    assert "source: FRED" in out
    assert "last CPI 2.60% YoY" in out
    assert "UNAVAILABLE" not in out


def test_macro_strip_skips_missing_fields():
    facts = {"today": "2026-06-26", "fed_funds_latest": 4.33}
    out = render_macro_facts_strip(facts)
    assert "Fed funds 4.33%" in out
    assert "10Y" not in out          # absent yields omitted, not guessed
    assert "CPI" not in out
    assert "UNAVAILABLE" not in out


def test_macro_strip_empty_facts_returns_blank():
    assert render_macro_facts_strip({}) == ""


def test_macro_strip_is_exported_from_package():
    from carrac.grounding import render_macro_facts_strip as exported
    assert callable(exported)
