"""Render-test for the facts-thick spotlight card + cross-asset board.

Builds a couple of realistic facts bundles, runs them through the REAL
``carrac.facts.card`` / ``carrac.facts.sentences`` / ``carrac.facts.labels``
helpers to produce the exact ``cards`` + ``board`` context the editorial route
passes, then renders ``templates/partials/_facts_modules.html`` standalone via a
minimal Jinja2 environment (autoescape on) and asserts the markup contains every
expected piece — and that the render never raises.

No app import, no Redis, no DB: the partial is rendered in isolation so this is
a fast, pure template/context contract test.
"""
from __future__ import annotations

import pathlib

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

from carrac.facts.card import level_flips, state_rows
from carrac.facts.labels import asset_name
from carrac.facts.ladder import ladder
from carrac.facts.sentences import asset_lede
from carrac.facts.translate import NOTABLE_BANDS

REPO_ROOT = pathlib.Path(__file__).parents[1]
TEMPLATES = REPO_ROOT / "templates"


# --------------------------------------------------------------------------- bundles

def _btc_bundle() -> dict:
    """A bullish-stacked BTC with a decisive level flip + confluence clusters."""
    return {
        "symbol": "BTC",
        "as_of": "2026-06-27T06:00:00",
        "price": 67000.0,
        "ma_stack": {"state": "bullish", "bars_held": 12, "cross": "none"},
        "rsi": {"value": 64.0, "zone": "neutral"},
        "stretch": {"mayer": 1.18, "mayer_pct": 61},
        "streaks": [
            {"type": "defending", "level": "sma50", "price": 65000.0, "bars": 5},
        ],
        "decisive_close": [
            {"level": "pdh", "direction": "above", "confirmed": True, "bars_ago": 1},
        ],
        "levels": {
            "clusters": [
                {"side": "resistance", "price": 69000.0, "dist_pct": 3.0,
                 "count": 3, "members": ["pwh", "r1", "sma20"]},
                {"side": "support", "price": 64000.0, "dist_pct": -4.5,
                 "count": 2, "members": ["sma50", "pml"]},
            ],
        },
        "range_pctile": {"pct": 72, "window_days": 90},
        "weekly": {
            "ma_stack": {"state": "bullish", "bars_held": 8},
            "streaks": [
                {"type": "defending", "level": "pwl", "price": 60000.0, "bars": 4},
            ],
            "decisive_close": [],
        },
    }


def _eth_bundle() -> dict:
    """A weaker ETH — bearish-leaning with a lost level (down flip)."""
    return {
        "symbol": "ETH",
        "as_of": "2026-06-27T06:00:00",
        "price": 3120.0,
        "ma_stack": {"state": "bearish", "bars_held": 6, "cross": "none"},
        "rsi": {"value": 38.0, "zone": "neutral"},
        "decisive_close": [
            {"level": "sma200", "direction": "below", "confirmed": True, "bars_ago": 2},
        ],
        "levels": {
            "clusters": [
                {"side": "support", "price": 3000.0, "dist_pct": -3.8,
                 "count": 2, "members": ["pml", "s1"]},
            ],
        },
        "range_pctile": {"pct": 28, "window_days": 90},
    }


def _btc_conf() -> dict:
    """A scanner-shaped confluence payload (get_symbol_confluence output) for BTC."""
    return {
        "symbol": "BTC", "price": 67000.0,
        "resistances": [
            {"price": 69000.0, "strength": 8.0, "tier": "strong", "side": "resistance",
             "count": 3, "members": [
                 {"code": "SMA200", "name": "200 SMA", "weight": 3.0},
                 {"code": "R1", "name": "Pivot R1", "weight": 1.5},
                 {"code": "PWH", "name": "Prev Week High", "weight": 1.0},
             ]},
        ],
        "supports": [
            {"price": 64000.0, "strength": 4.0, "tier": "medium", "side": "support",
             "count": 2, "members": [
                 {"code": "SMA50", "name": "50 SMA", "weight": 2.0},
                 {"code": "PML", "name": "Prev Month Low", "weight": 1.0},
             ]},
        ],
    }


def _card_ctx(symbol: str, bundle: dict, conf: dict | None = None) -> dict:
    """Mirror the route: build a spotlight card dict from the real helpers."""
    return {
        "name": asset_name(symbol),
        "ticker": symbol,
        "lede": asset_lede(bundle),
        "state_rows": state_rows(bundle),
        "flips": level_flips(bundle),
        "ladder": ladder(conf) if conf else None,
    }


def _render() -> tuple[str, dict, list]:
    btc, eth = _btc_bundle(), _eth_bundle()
    cards = [_card_ctx("BTC", btc, _btc_conf()), _card_ctx("ETH", eth)]
    board = [
        {
            "name": "Crypto",
            "assets": [
                {"name": asset_name("BTC"), "ticker": "BTC", "note": asset_lede(btc), "pct": 85,
                 "wall": {"side": "res", "word": "ceiling", "dist_pct": 0.9, "signal_count": 8}},
                {"name": asset_name("ETH"), "ticker": "ETH", "note": asset_lede(eth), "pct": 10},
            ],
        },
        {
            "name": "Equities",
            "assets": [
                {"name": asset_name("SPX"), "ticker": "SPX", "note": None, "pct": None},
            ],
        },
    ]

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    tpl = env.get_template("partials/_facts_modules.html")
    html = tpl.render(cards=cards, board=board, notable_bands=NOTABLE_BANDS)
    return html, cards, board


# --------------------------------------------------------------------------- tests

def test_render_does_not_raise():
    html, _, _ = _render()
    assert isinstance(html, str) and html.strip()


def test_card_pieces_present():
    html, cards, _ = _render()

    # Asset name (Fraunces heading) + muted ticker badge.
    assert "Bitcoin" in html
    assert 'class="tk"' in html and ">BTC<" in html

    # Lede — the one-line read (escaped exactly as Jinja autoescape emits it).
    lede = cards[0]["lede"]
    assert lede, "BTC bundle must produce a lede sentence"
    assert str(escape(lede)) in html

    # "Current state" label-first rows (state_rows): srow / slab / sval.
    assert "Current state" in html
    assert 'class="srow"' in html
    assert 'class="slab"' in html and 'class="sval"' in html
    assert "Full bull stack" in html  # Trend value rendered via state_rows

    # Levels & flips — at least one daily flip with directional colour.
    assert "Levels &amp; flips" in html
    assert 'class="flip up"' in html          # decisive close ABOVE → up
    assert 'class="flip dn"' in html          # ETH lost the 200-day → down
    flip_text = cards[0]["flips"]["daily"][0]["text"]
    assert str(escape(flip_text)) in html
    assert ">D<" in html and ">W<" in html    # daily + weekly tf tags

    # Key levels — the spatial ladder: a zone with a strength bar, tier word,
    # editorial member name, and the zone price.
    assert "Key levels" in html
    assert 'class="zone strong res"' in html  # BTC's strong resistance wall
    assert 'class="zbar"' in html             # the strength bar
    assert "Strong wall" in html              # tier headline word
    assert "200-Day Average" in html          # editorial member name (heaviest-weight first)
    assert "$69,000.00" in html               # ladder zone price
    assert "price now" in html                # the price-now divider


def test_board_pieces_present():
    html, _, _ = _render()

    # Bordered board table with cluster group-rows + a per-asset note row.
    assert 'class="board"' in html
    assert 'class="grp"' in html
    assert ">Crypto<" in html and ">Equities<" in html
    assert 'class="note"' in html
    # Asset with no lede falls back to an em-dash placeholder.
    assert "—" in html
    # Real thead + decile range-percentile wash (BTC p85 → warm 'ph', ETH p10 → cool 'pl').
    assert "<thead>" in html
    assert 'class="ph"' in html and 'class="pl"' in html
    # Quiet-row wall tag on a board row (BTC has a strong ceiling near price).
    assert 'class="qwall res"' in html
    assert "Strong wall ceiling" in html and "8 signals" in html


def test_absent_context_renders_empty():
    """No cards/board → the spotlight + board blocks simply don't appear."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    tpl = env.get_template("partials/_facts_modules.html")
    html = tpl.render()  # everything undefined / falsy
    assert "Spotlight" not in html
    assert 'class="board"' not in html
