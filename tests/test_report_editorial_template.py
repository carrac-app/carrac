"""Template-source assertions for the editorial morning report (admin-gated redesign).

Guards three GA-blockers on templates/morning_report_editorial.html:
1. Inline non-advice disclaimer (mirrors the classic template's report-disclaimer).
2. PostHog analytics init parity with the classic template.
3. No dead stub buttons (PDF href="#"; Listen href="#" with no JS wiring).
"""
from pathlib import Path

TPL = Path("templates/morning_report_editorial.html").read_text()


def test_editorial_has_inline_disclaimer():
    assert "not financial advice" in TPL
    assert "legal.disclaimer" in TPL


def test_editorial_has_posthog_init():
    assert "_posthog_init.html" in TPL


def test_editorial_has_no_dead_pdf_button():
    assert ">PDF</a>" not in TPL


def test_editorial_has_no_dead_listen_button():
    """The Listen button is href="#" with no JS handler anywhere in the template
    (grepped the whole inline <script> block for '.listen' / 'listen' wiring —
    none found), so it should be removed rather than shipped as a dead stub."""
    assert ">Listen<" not in TPL
    assert "btn listen" not in TPL


# ---------------------------------------------------------------------------
# Editorial v4 — linear desk note (single-column, tabs out, cluster spotlight)
# ---------------------------------------------------------------------------

PARTIAL = Path("templates/partials/_facts_modules.html").read_text()
EDITORIAL = Path("templates/morning_report_editorial.html").read_text()


def test_no_two_column_grid():
    assert 'class="cols"' not in PARTIAL


def test_clusters_spotlight_present_with_guard():
    assert "clusters is defined" in PARTIAL and "levels_summary" in PARTIAL


def test_ladder_and_flips_collapsed():
    # full ladder + level flips live inside <details> now
    assert PARTIAL.count("<details") >= 1 and "ladder_block" in PARTIAL


def test_editorial_tabs_gone():
    assert "tab-pane" not in EDITORIAL and 'id="ed-take"' in EDITORIAL  # The Tape survives, tabs don't


# ---------------------------------------------------------------------------
# Editorial bands R1 — Docket · Liquidity & Credit · Breadth · Regime clock
#
# Contract: every band's context var may be absent entirely (Jinja Undefined,
# as when /assets pages or the older tests render the partial without these
# keys), None, or [] — in every case the band must render NOTHING (no eyebrow,
# no empty shell). The Docket band must never carry a consensus/expected/
# forecast column or word anywhere (Global Constraints, bulletproof rule).
# ---------------------------------------------------------------------------

from jinja2 import Environment, FileSystemLoader, select_autoescape

_ENV = Environment(
    loader=FileSystemLoader(str(Path("templates"))),
    autoescape=select_autoescape(["html"]),
)


def _render_partial(**ctx):
    return _ENV.get_template("partials/_facts_modules.html").render(**ctx)


_DOCKET_SAMPLE = [
    {"when": "2026-07-03 12:30 UTC", "title": "CPI (June)", "impact": "high",
     "actual": "2.4% YoY", "history": None},
    {"when": "2026-07-04 14:00 UTC", "title": "ISM Services", "impact": "medium",
     "actual": None, "history": None},
]

_LIQUIDITY_SAMPLE = [
    {"label": "High-yield spread", "value": "3.1%", "zscore": 1.8,
     "percentile": 92, "meaning": "wider than 92% of the last 90 days"},
    {"label": "Reverse repo", "value": "$412B", "zscore": None,
     "percentile": None, "meaning": None},
]

_BREADTH_SAMPLE = [
    {"label": "Above 200-day", "value": "62%", "tone": "warm"},
    {"label": "New lows (90d)", "value": "4", "tone": "cold"},
]

_REGIME_CLOCK_SAMPLE = {"state": "Favorable", "day_in_state": 79,
                         "median_run_days": 64, "note": None}


def test_docket_band_guarded_with_no_consensus_language():
    assert "docket is defined" in PARTIAL
    for banned in ("expected", "consensus", "forecast"):
        assert banned not in PARTIAL.lower(), f"Docket must never carry a {banned!r} column/copy"


def test_liquidity_band_guarded():
    assert "liquidity is defined" in PARTIAL


def test_breadth_band_guarded():
    assert "breadth is defined" in PARTIAL


def test_regime_clock_guarded():
    assert "regime_clock is defined" in PARTIAL


def test_secnav_has_docket_and_liquidity_anchors():
    assert '#ed-docket' in EDITORIAL
    assert '#ed-liquidity' in EDITORIAL


def test_docket_band_renders_when_present_no_consensus_column():
    html = _render_partial(docket=_DOCKET_SAMPLE)
    assert "CPI (June)" in html
    assert "2.4% YoY" in html
    assert "ISM Services" in html
    for banned in ("expected", "consensus", "forecast"):
        assert banned not in html.lower()


def test_docket_band_absent_when_undefined_none_or_empty():
    for kwargs in ({}, {"docket": None}, {"docket": []}):
        html = _render_partial(**kwargs)
        assert 'id="ed-docket"' not in html
        assert "CPI" not in html


def test_liquidity_band_renders_and_tolerates_missing_fields():
    html = _render_partial(liquidity=_LIQUIDITY_SAMPLE)
    assert "High-yield spread" in html
    assert "wider than 92% of the last 90 days" in html
    assert "Reverse repo" in html  # renders fine without zscore/percentile/meaning


def test_liquidity_band_absent_when_undefined_none_or_empty():
    for kwargs in ({}, {"liquidity": None}, {"liquidity": []}):
        html = _render_partial(**kwargs)
        assert 'id="ed-liquidity"' not in html
        assert "High-yield spread" not in html


def test_breadth_band_renders_with_tone():
    html = _render_partial(breadth=_BREADTH_SAMPLE)
    assert "Above 200-day" in html
    assert "62%" in html
    assert "New lows" in html


def test_breadth_band_absent_when_undefined_none_or_empty():
    for kwargs in ({}, {"breadth": None}, {"breadth": []}):
        html = _render_partial(**kwargs)
        assert "Above 200-day" not in html


def test_regime_clock_suffix_renders_inside_regime_banner():
    html = _render_partial(
        regime={"state": "favorable", "stay_prob": 0.62, "duration_days": 12},
        regime_clock=_REGIME_CLOCK_SAMPLE,
    )
    assert "day 79" in html
    assert "64-day" in html


def test_regime_clock_absent_does_not_crash_or_render():
    for kwargs in (
        {"regime": {"state": "favorable"}},
        {"regime": {"state": "favorable"}, "regime_clock": None},
        {"regime_clock": _REGIME_CLOCK_SAMPLE},  # no `regime` at all → banner itself absent
    ):
        html = _render_partial(**kwargs)
        assert "day 79" not in html


def test_all_four_bands_render_together_without_raising():
    html = _render_partial(
        docket=_DOCKET_SAMPLE,
        liquidity=_LIQUIDITY_SAMPLE,
        breadth=_BREADTH_SAMPLE,
        regime={"state": "favorable", "duration_days": 12},
        regime_clock=_REGIME_CLOCK_SAMPLE,
    )
    assert isinstance(html, str) and html.strip()


def test_all_four_bands_missing_renders_no_empty_shells():
    """No context at all (Jinja Undefined for every new var) must produce zero
    trace of any of the four new bands — matching /assets and legacy callers
    that never pass these keys."""
    html = _render_partial(facts_list=[])
    for marker in ('id="ed-docket"', 'id="ed-liquidity"', "Today’s docket",
                   "Liquidity &amp; credit", "Breadth internals", "day 79"):
        assert marker not in html


# ---------------------------------------------------------------------------
# Editorial bands R2 — Rotation strip · Flows & Attention
#
# Same contract shape as R1: `rotation`/`flows` may be absent entirely (Jinja
# Undefined), None, or [] — in every case the band must render NOTHING (no
# eyebrow, no empty shell). Rotation renders between the board and the
# signals bands; Flows joins the signals bands after breadth.
# ---------------------------------------------------------------------------

_ROTATION_SAMPLE = [
    {"quadrant": "Leading", "assets": [
        {"ticker": "GOLD", "name": "Gold"}, {"ticker": "SILVER", "name": "Silver"},
    ]},
    {"quadrant": "Improving", "assets": [{"ticker": "BTC", "name": "Bitcoin"}]},
    {"quadrant": "Weakening", "assets": [{"ticker": "SPX", "name": "S&P 500"}]},
    {"quadrant": "Lagging", "assets": [{"ticker": "OIL", "name": "Crude Oil"}]},
]

_FLOWS_SAMPLE = [
    {"label": "BTC ETF net flow (day)", "value": "+$142M", "zscore": 0.9,
     "percentile": 71.0, "meaning": "in the top 29% of the last 90 days"},
    {"label": "Stablecoin dominance", "value": "6.2%", "zscore": None,
     "percentile": None, "meaning": None},
]


def test_rotation_band_guarded():
    assert "rotation is defined" in PARTIAL


def test_flows_band_guarded():
    assert "flows is defined" in PARTIAL


def test_secnav_has_rotation_and_flows_anchors():
    assert '#ed-rotation' in EDITORIAL
    assert '#ed-flows' in EDITORIAL


def test_rotation_band_renders_quadrants_in_fixed_order():
    html = _render_partial(rotation=_ROTATION_SAMPLE)
    assert 'id="ed-rotation"' in html
    for label in ("Leading", "Improving", "Weakening", "Lagging"):
        assert label in html
    assert "Gold" in html and "GOLD" in html
    assert (html.index("Leading") < html.index("Improving")
            < html.index("Weakening") < html.index("Lagging"))


def test_rotation_band_omits_empty_quadrant_rows():
    rotation = [
        {"quadrant": "Leading", "assets": [{"ticker": "GOLD", "name": "Gold"}]},
        {"quadrant": "Improving", "assets": []},
    ]
    html = _render_partial(rotation=rotation)
    assert "Leading" in html
    assert "Improving" not in html


def test_rotation_band_absent_when_undefined_none_or_empty():
    for kwargs in ({}, {"rotation": None}, {"rotation": []}):
        html = _render_partial(**kwargs)
        assert 'id="ed-rotation"' not in html
        assert "GOLD" not in html


def test_flows_band_renders_and_tolerates_missing_fields():
    html = _render_partial(flows=_FLOWS_SAMPLE)
    assert 'id="ed-flows"' in html
    assert "BTC ETF net flow" in html
    assert "+$142M" in html
    assert "in the top 29% of the last 90 days" in html
    assert "Stablecoin dominance" in html  # renders fine without zscore/percentile/meaning


def test_flows_band_absent_when_undefined_none_or_empty():
    for kwargs in ({}, {"flows": None}, {"flows": []}):
        html = _render_partial(**kwargs)
        assert 'id="ed-flows"' not in html
        assert "BTC ETF net flow" not in html


def test_flows_band_joins_signals_after_breadth():
    html = _render_partial(breadth=_BREADTH_SAMPLE, flows=_FLOWS_SAMPLE)
    assert html.index("Breadth internals") < html.index('id="ed-flows"')


def test_rotation_and_flows_render_together_without_raising():
    html = _render_partial(
        docket=_DOCKET_SAMPLE,
        liquidity=_LIQUIDITY_SAMPLE,
        breadth=_BREADTH_SAMPLE,
        regime={"state": "favorable", "duration_days": 12},
        regime_clock=_REGIME_CLOCK_SAMPLE,
        rotation=_ROTATION_SAMPLE,
        flows=_FLOWS_SAMPLE,
    )
    assert isinstance(html, str) and html.strip()


def test_rotation_and_flows_missing_renders_no_empty_shells():
    html = _render_partial(facts_list=[])
    for marker in ('id="ed-rotation"', 'id="ed-flows"', "Rotation", "Flows &amp; attention"):
        assert marker not in html


# ---------------------------------------------------------------------------
# Editorial bands R3 — Docket history sub-line · "History says" spotlight line
#
# Same contract discipline as R1/R2: `ev.get('history')` (docket rows) and
# `card.get('history_says')` (spotlight clusters) may be absent, None, or an
# incomplete dict — in every case the sub-line must render NOTHING. Language
# must be strictly historical-descriptive, past tense, never predictive/
# advisory (Global Constraints bulletproof rule; mirrors the Scenarios
# phrasing rules in morning_engine/prompts.py).
# ---------------------------------------------------------------------------

_CLUSTERS_SAMPLE = [
    {
        "category": "Crypto",
        "label": "Crypto",
        "b": {"symbol": "BTC", "name": "Bitcoin", "price": 67000.0,
              "as_of": "2026-07-02T06:00:00"},
        "lede": "BTC holds a bullish stack.",
        "state_rows": [],
        "flips": {},
        "ladder": None,
        "levels_summary": None,
        "quant": None,
        "history_says": {
            "n": 41, "horizon_days": 7, "median_move_pct": 2.3,
            "condition_label": "RSI oversold + bottom-quartile range",
        },
    },
]


def test_docket_row_shows_history_subline_when_present():
    docket = [
        {"when": "2026-07-03 12:30 UTC", "title": "CPI (June)", "impact": "high",
         "actual": "2.4% YoY",
         "history": "Last 14 CPI prints: SPX median absolute next-day move 1.1%"},
    ]
    html = _render_partial(docket=docket)
    assert 'class="dhist"' in html
    assert "Last 14 CPI prints: SPX median absolute next-day move 1.1%" in html


def test_docket_row_omits_history_subline_when_none():
    # _DOCKET_SAMPLE rows both carry history: None
    html = _render_partial(docket=_DOCKET_SAMPLE)
    assert 'class="dhist"' not in html


def test_docket_row_omits_history_subline_when_missing_or_empty_string():
    for hist in (None, ""):
        docket = [{"when": "2026-07-03 12:30 UTC", "title": "CPI (June)",
                   "impact": "high", "actual": "2.4% YoY", "history": hist}]
        html = _render_partial(docket=docket)
        assert 'class="dhist"' not in html
    docket_no_key = [{"when": "2026-07-03 12:30 UTC", "title": "CPI (June)",
                       "impact": "high", "actual": "2.4% YoY"}]
    html = _render_partial(docket=docket_no_key)
    assert 'class="dhist"' not in html


def test_history_says_line_renders_with_exact_copy():
    html = _render_partial(clusters=_CLUSTERS_SAMPLE)
    assert 'class="histline"' in html
    assert ("History says · In 41 similar instances "
            "(RSI oversold + bottom-quartile range), "
            "the median 7-day move was +2.3%.") in html


def test_history_says_line_signs_negative_moves():
    card = dict(_CLUSTERS_SAMPLE[0])
    card["history_says"] = {
        "n": 30, "horizon_days": 14, "median_move_pct": -1.6,
        "condition_label": "RSI overbought",
    }
    html = _render_partial(clusters=[card])
    assert "the median 14-day move was -1.6%." in html


def test_history_says_line_absent_when_missing_none_or_incomplete():
    base = _CLUSTERS_SAMPLE[0]
    incomplete_variants = (
        None,
        {},
        {"n": 41, "horizon_days": 7, "median_move_pct": 2.3},  # no condition_label
        {"horizon_days": 7, "median_move_pct": 2.3, "condition_label": "x"},  # no n
        {"n": 41, "median_move_pct": 2.3, "condition_label": "x"},  # no horizon_days
        {"n": 41, "horizon_days": 7, "condition_label": "x"},  # no median_move_pct
    )
    for hs in incomplete_variants:
        card = dict(base)
        card["history_says"] = hs
        html = _render_partial(clusters=[card])
        assert 'class="histline"' not in html
        assert "History says" not in html

    # Key entirely absent from the card dict.
    card_no_key = {k: v for k, v in base.items() if k != "history_says"}
    html = _render_partial(clusters=[card_no_key])
    assert 'class="histline"' not in html
    assert "History says" not in html


def test_history_says_line_is_descriptive_past_tense_no_advice_language():
    html = _render_partial(clusters=_CLUSTERS_SAMPLE)
    assert "History says" in html
    for banned in ("should", "recommend", "buy", "sell", "you should", "i would"):
        assert banned not in html.lower()


# ---------------------------------------------------------------------------
# Public /brief (templates half)
#
# Anonymous-mode support: an inline window.__briefData blob (guarded, only
# emitted when brief_inline is supplied), a public_brief-gated <head> meta
# block (title/description/OG/Twitter/canonical), the 4 existing fetch sites
# (sentiment/breadth/snapshot ribbon + the main report pipeline) sourcing from
# the inline blob when present via the SAME handler functions (no logic
# forks), and the afternoon-delta fetch skipped entirely under __briefData.
# Also: the landing page's report_teaser CTA retargets to /brief.
#
# These are source-text assertions (like the rest of this file) rather than a
# full Jinja render, because the real template calls url_for()/cache_version()
# which need a live Flask app context — the admin ?editorial=1 path (no
# brief_inline/public_brief passed) must keep rendering byte-identical to
# today, which the `is defined` guards below verify at the source level.
# ---------------------------------------------------------------------------

def test_briefdata_inline_script_guarded():
    assert "brief_inline is defined and brief_inline" in EDITORIAL
    assert "window.__briefData = {{ brief_inline | tojson }}" in EDITORIAL


def test_briefdata_inline_script_has_csp_nonce():
    assert '<script nonce="{{ csp_nonce | default(\'\') }}">window.__briefData' in EDITORIAL


def test_public_brief_meta_block_guarded():
    assert "public_brief is defined and public_brief" in EDITORIAL


def test_public_brief_meta_block_has_title_og_and_canonical():
    assert "Carrac — The Daily Brief" in EDITORIAL
    assert "{{ site_url }}/brief" in EDITORIAL
    assert 'property="og:title"' in EDITORIAL
    assert 'property="og:description"' in EDITORIAL
    assert 'property="og:type" content="article"' in EDITORIAL
    assert 'rel="canonical"' in EDITORIAL
    assert 'name="twitter:card"' in EDITORIAL


def test_public_brief_meta_reuses_existing_og_image():
    # landing_v5.html / _marketing_base_v5.html already carry an og:image —
    # reuse it rather than shipping a dedicated brief card image.
    assert "og-dashboard.png" in EDITORIAL


def test_default_title_unchanged_when_public_brief_not_set():
    assert "Carrac · Morning Briefing" in EDITORIAL


def test_afternoon_fetch_skipped_under_briefdata():
    assert "if (!window.__briefData)" in EDITORIAL


def test_four_fetch_sites_source_from_briefdata_when_present():
    assert "_brief.sentiment" in EDITORIAL
    assert "_brief.breadth" in EDITORIAL
    assert "_brief.snapshot" in EDITORIAL
    assert "window.__briefData.report" in EDITORIAL or "_brief.report" in EDITORIAL


def test_fetch_handlers_extracted_and_reused_no_logic_forks():
    """Each fetch site must reuse ONE named handler function whether sourced
    from window.__briefData or a live fetch — no duplicated parsing logic
    between the two paths."""
    for fn in ("_applySentimentRibbon", "_applyBreadthRibbon",
               "_applyBondVolRibbon", "_applyMorningReport"):
        assert EDITORIAL.count("function " + fn + "(data)") == 1, fn



# ---------------------------------------------------------------------------
# Derivatives Desk band + lockbox() paywall macro
#
# CONTRACT: `desk` arrives already tier-resolved AND REDACTED by the builder —
# `row.value` is None on every locked, non-free row. This template's ONLY job
# is to render "•••" for None and show the lockbox CTA when `not desk.unlocked`
# — it must NEVER be the thing doing the redaction. The no-leak render test
# below is the mandatory guard for that rule. Band sits after Rotation, before
# the signals bands (#ed-signals).
# ---------------------------------------------------------------------------

_DESK_SAMPLE_UNLOCKED = {
    "as_of": "2026-07-02",
    "unlocked": True,
    "cta_href": "/plans",
    "assets": [
        {"ticker": "BTC", "name": "Bitcoin", "rows": [
            {"label": "DVOL", "value": "40.6", "hint": "Deribit 30-day implied-volatility index", "free": True},
            {"label": "25Δ skew", "value": "-2.1", "hint": "put-call implied-vol skew", "free": False},
            {"label": "Basis (annualized)", "value": "+6.8%", "hint": None, "free": False},
        ]},
        {"ticker": "ETH", "name": "Ethereum", "rows": [
            {"label": "DVOL", "value": "58.2", "hint": "Deribit 30-day implied-volatility index", "free": True},
        ]},
    ],
}

_DESK_SAMPLE_LOCKED = {
    "as_of": "2026-07-02",
    "unlocked": False,
    "cta_href": "/plans",
    "assets": [
        {"ticker": "BTC", "name": "Bitcoin", "rows": [
            {"label": "DVOL", "value": "40.6", "hint": "Deribit 30-day implied-volatility index", "free": True},
            {"label": "25Δ skew", "value": None, "hint": "put-call implied-vol skew", "free": False},
            {"label": "Basis (annualized)", "value": None, "hint": None, "free": False},
        ]},
        {"ticker": "ETH", "name": "Ethereum", "rows": [
            {"label": "DVOL", "value": "58.2", "hint": "Deribit 30-day implied-volatility index", "free": True},
            {"label": "25Δ skew", "value": None, "hint": None, "free": False},
        ]},
    ],
}


def test_desk_band_guarded():
    assert "desk is defined" in PARTIAL


def test_desk_band_absent_when_undefined_none_or_empty():
    for kwargs in ({}, {"desk": None}, {"desk": {}}):
        html = _render_partial(**kwargs)
        assert 'id="ed-desk"' not in html
        assert "Derivatives Desk" not in html


def test_secnav_has_desk_anchor():
    assert '#ed-desk' in EDITORIAL


def test_desk_band_placed_after_rotation_before_signals():
    html = _render_partial(
        rotation=_ROTATION_SAMPLE,
        desk=_DESK_SAMPLE_UNLOCKED,
        cot=[{"label": "Net non-commercial", "value": "+12,400"}],
    )
    assert (html.index('id="ed-rotation"')
            < html.index('id="ed-desk"')
            < html.index('id="ed-signals"'))


def test_desk_band_unlocked_renders_as_of_and_all_values():
    html = _render_partial(desk=_DESK_SAMPLE_UNLOCKED)
    assert 'id="ed-desk"' in html
    assert "2026-07-02" in html
    assert "BTC" in html and "Bitcoin" in html
    assert "ETH" in html and "Ethereum" in html
    assert "40.6" in html and "58.2" in html
    assert "-2.1" in html and "+6.8%" in html
    assert 'class="lockbox"' not in html  # unlocked → no paywall wrapper


def test_desk_band_locked_shows_redacted_placeholder_and_cta():
    html = _render_partial(desk=_DESK_SAMPLE_LOCKED)
    assert 'class="redact"' in html
    assert "•••" in html  # "•••"
    assert 'class="lockbox"' in html
    assert "An admin can change plans under Admin" in html


def test_desk_band_locked_keeps_only_free_rows_visible():
    html = _render_partial(desk=_DESK_SAMPLE_LOCKED)
    # DVOL is the free hook row on both assets — real value stays visible.
    assert "40.6" in html
    assert "58.2" in html
    # Non-free rows are already None from the builder — no real numbers leak.
    assert "-2.1" not in html
    assert "+6.8%" not in html


def test_desk_no_leak_locked_render_lacks_sentinel_unlocked_contains_it():
    """The paywall's integrity: a sentinel value present in the unlocked/full
    dict must NEVER appear in the locked render (value=None on that row in the
    locked variant, per the redact-in-the-builder contract)."""
    full = {
        "as_of": "2026-07-02", "unlocked": True, "cta_href": "/plans",
        "assets": [{"ticker": "BTC", "name": "Bitcoin", "rows": [
            {"label": "DVOL", "value": "40.6", "hint": None, "free": True},
            {"label": "25Δ skew", "value": "SENTINEL_LEAK_42", "hint": None, "free": False},
        ]}],
    }
    locked = {
        "as_of": "2026-07-02", "unlocked": False, "cta_href": "/plans",
        "assets": [{"ticker": "BTC", "name": "Bitcoin", "rows": [
            {"label": "DVOL", "value": "40.6", "hint": None, "free": True},
            {"label": "25Δ skew", "value": None, "hint": None, "free": False},
        ]}],
    }
    html_unlocked = _render_partial(desk=full)
    html_locked = _render_partial(desk=locked)
    assert "SENTINEL_LEAK_42" in html_unlocked
    assert "SENTINEL_LEAK_42" not in html_locked


def test_lockbox_macro_reusable_with_arbitrary_body():
    """lockbox() is v1 of the reusable paywall component — it must accept and
    wrap ARBITRARY caller-block content, not just Derivatives Desk markup."""
    tpl_src = (
        '{% from "partials/_facts_modules.html" import lockbox %}'
        '{% call lockbox("Sample Title", "Sample subtitle", "/plans", "Unlock") %}'
        '<div class="arbitrary-body">Arbitrary content ABC123</div>'
        '{% endcall %}'
    )
    html = _ENV.from_string(tpl_src).render()
    assert "Arbitrary content ABC123" in html
    assert "Sample Title" in html
    assert "Sample subtitle" in html
    assert 'href="/plans"' in html
    assert ">Unlock<" in html
    assert 'class="lockbox"' in html
