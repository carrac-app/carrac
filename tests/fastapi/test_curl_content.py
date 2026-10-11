import pytest
import carrac.curl_charts.content as content


@pytest.fixture(autouse=True)
def _hosted_site_url(monkeypatch):
    """These tests pin page structure/copy against the hosted instance's
    address; the generated links follow SITE_URL, so say which instance."""
    monkeypatch.setenv("SITE_URL", "https://carrac.cc")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


def test_funnel_footer_lines():
    f = content.funnel_footer(plain=True)
    assert "carrac.cc" in f
    assert "carrac.cc/mcp" in f
    assert "\x1b" not in f


def test_funnel_footer_colored():
    assert "\x1b" in content.funnel_footer(plain=False)


def test_help_page_mentions_grammar():
    h = content.help_page()
    for needle in ("@10d", "?ema", "cols=", ":assets", "?T"):
        assert needle in h, needle


def test_overview_table(monkeypatch):
    monkeypatch.setattr(
        content,
        "_prices",
        lambda: {
            "BTC": {"price": 61665.9, "change_pct": -3.98},
            "ETH": {"price": 1640.61, "change_pct": -8.59},
        },
    )
    t = content.overview_table(plain=True)
    assert "BTC" in t and "61665.9" in t and "-3.98%" in t
    assert "curl carrac.cc/btc" in t  # usage examples present


def test_unknown_symbol_message():
    m = content.unknown_symbol("xyz", plain=True)
    assert "xyz" in m and ":assets" in m


def test_assets_page_lists_symbols():
    a = content.assets_page(["BTC", "ETH", "SPX"])
    assert "btc" in a and "spx" in a


def test_funnel_footer_ends_with_newline():
    assert content.funnel_footer(plain=True).endswith("\n")
    assert content.funnel_footer(plain=False).endswith("\n")


def test_rate_limited_mentions_carrac():
    m = content.rate_limited(plain=True)
    assert "carrac.cc" in m and m.endswith("\n")


def test_overview_table_survives_none_price(monkeypatch):
    monkeypatch.setattr(
        content, "_prices",
        lambda: {"BTC": {"price": None, "change_pct": None}},
    )
    t = content.overview_table(plain=True)
    assert "BTC" in t  # row renders, no TypeError
