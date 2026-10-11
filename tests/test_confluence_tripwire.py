from carrac.grounding.hallucination_scan import scan_confluence_fabrication

# The two _confluence_tripwire tests below request `notifications`: the
# tripwire _notify()s on a hit, and the notification-send recorder fails any
# test that sends without opting in.

FOCUS = [{"asset": "BTC", "price": 62890,
          "support": {"price": 62020}, "resistance": {"price": 66008}}]


def test_clean_when_levels_match():
    txt = "BTC sits above support at 62,020 with resistance at 66,008."
    assert scan_confluence_fabrication(txt, FOCUS) == []


def test_flags_invented_level():
    txt = "BTC support now sits at 58,500."
    out = scan_confluence_fabrication(txt, FOCUS)
    assert out and out[0]["type"] == "fabricated_level"


def test_ignores_non_level_numbers():
    # "200-day" and a week count must NOT be flagged — they aren't price levels
    txt = "BTC support at 62,020 is back above its 200-day moving average after 3 weeks."
    assert scan_confluence_fabrication(txt, FOCUS) == []


def test_no_focus_no_crash():
    assert scan_confluence_fabrication("BTC support at 1,234", []) == []


# --- Direct table-awareness unit tests for scan_confluence_fabrication -------

_TABLE_FOCUS = [
    {"asset": "BTC", "price": 59834,
     "support": {"price": 59130}, "resistance": {"price": 60013}},
]

_CLEAN_TABLE = (
    "## LEVELS IN FOCUS\n"
    "| Asset | Price | Nearest support | Nearest resistance |\n"
    "|---|---|---|---|\n"
    "| BTC | 59,834 | 59,130 (1.2% below, faint) | 60,013 (0.3% above, faint) |\n"
)

_BAD_TABLE = _CLEAN_TABLE.replace(
    "60,013 (0.3% above, faint)", "63,500 (bogus)"
)


def test_scan_confluence_fabrication_clean_table_passes():
    """A LiF table with all numbers grounded should return no findings."""
    assert scan_confluence_fabrication(_CLEAN_TABLE, _TABLE_FOCUS) == []


def test_scan_confluence_fabrication_bad_table_flags():
    """A LiF table with one bogus number should flag fabricated_level."""
    out = scan_confluence_fabrication(_BAD_TABLE, _TABLE_FOCUS)
    assert out and out[0]["type"] == "fabricated_level"


def test_scan_ignores_percentages_in_prose():
    """Percentages in prose must NOT be flagged as fabricated price levels.

    1.9 falls in the magnitude band [1.6, 6.4] for NATGAS at 3.20 but is NOT
    grounded; with % present it is skipped entirely.
    """
    focus = [{"asset": "NATGAS", "price": 3.20, "support": {"price": 3.18}, "resistance": {"price": 3.24}}]
    sec = ("## LEVELS IN FOCUS\n"
           "NATGAS support sits inside a tight 1.9% range between two strong levels.\n")
    assert scan_confluence_fabrication(sec, focus) == []   # 1.9% is a percentage, not a level


def test_scan_still_flags_real_invented_level_low_priced():
    """Ungrounded numbers in the price band must still be flagged (not a %)."""
    focus = [{"asset": "NATGAS", "price": 3.20, "support": {"price": 3.18}, "resistance": {"price": 3.24}}]
    sec = ("## LEVELS IN FOCUS\n"
           "NATGAS resistance now sits at 5.50.\n")   # 5.50 in band [1.6,6.4], not grounded, no %
    out = scan_confluence_fabrication(sec, focus)
    assert out and out[0]["type"] == "fabricated_level"


# --- Precision fixes --------------------------------------------
# Replaying the scanner over the last 45 general reports fired on 16 of the 38
# that had a Levels-in-Focus section — 95 findings, every one a false positive.
# The three causes below account for all of them. Prose is verbatim from the
# real reports that produced the flags.

_R350_FOCUS = [
    {"asset": "SILVER", "price": 58.49,
     "support": {"price": 57.80}, "resistance": {"price": 58.66}},
    {"asset": "ETH", "price": 1861.73,
     "support": {"price": 1845.30}, "resistance": {"price": 1875.30}},
    {"asset": "SOYBEANS", "price": 1252.50,
     "support": {"price": 1242.00}, "resistance": {"price": 1270.15}},
]

# One real report paragraph, three assets, all levels correct.
_R350_PROSE = (
    "## Levels in Focus\n"
    "**SILVER** at $58.49 is sandwiched within a 1.5% band — just 0.3% below faint "
    "resistance at $58.66 and 1.2% above faint support at $57.80, reflecting the "
    "compression visible in its bearish EMA ribbon and death cross structure. "
    "**ETH** at $1,861.73 is 0.9% above a strong support cluster at $1,845.30 — the "
    "most structurally significant level in this table — with only a faint resistance "
    "layer at $1,875.30 separating it from the next meaningful zone. "
    "**SOYBEANS** (as noted, at the 97th percentile of its range) sits with faint "
    "support 0.8% below at $1,242.00 and faint resistance 1.4% above at $1,270.15.\n"
)


def test_prose_levels_attributed_to_the_nearest_preceding_asset():
    """Every level in this paragraph is correct — the scan must be silent.

    Checking each number against EVERY asset named on the line would flag
    SOYBEANS' real support (1,242.00) as an ETH fabrication and ETH's real
    price (1,861.73) as a SOYBEANS one: six flags, zero real problems.
    """
    assert scan_confluence_fabrication(_R350_PROSE, _R350_FOCUS) == []


def test_still_flags_a_fabrication_in_multi_asset_prose():
    """Attribution must not cost detection: break ETH's support, it must fire —
    and be reported against ETH, not against whichever asset shares the line."""
    bad = _R350_PROSE.replace("$1,845.30", "$1,799.00")
    out = scan_confluence_fabrication(bad, _R350_FOCUS)
    assert out and out[0]["type"] == "fabricated_level"
    assert all(f["detail"].startswith("ETH:") for f in out)


def test_level_named_before_its_asset_is_still_checked():
    """Attribution must not cost recall: a number ahead of every asset mention
    falls back to the first asset on the line rather than being dropped."""
    txt = "Fresh resistance at 58,500 now caps BTC."
    out = scan_confluence_fabrication(txt, FOCUS)
    assert out and out[0]["detail"].startswith("BTC:")


def test_ordinal_percentile_is_not_a_price_level():
    """'97th percentile' / '94th percentile' read as bare price levels because the
    percent-skip only looked for a literal '%' in the very next character."""
    focus = [{"asset": "SILVER", "price": 58.49,
              "support": {"price": 57.80}, "resistance": {"price": 58.66}}]
    sec = ("## Levels in Focus\n"
           "SILVER support holds while the range sits at the 97th percentile.\n")
    assert scan_confluence_fabrication(sec, focus) == []


def test_cpr_pivot_cited_in_prose_is_grounded_by_the_levels_set():
    """Prose legitimately cites CPR pivots and other computed levels ('the monthly
    S1 at $3,810', 'the CPR R1 of $6.59'). Grounding on nearest support/resistance
    /price alone flagged all of them. `levels` carries the full computed set."""
    focus = [{"asset": "GOLD", "price": 4018.80,
              "support": {"price": 3973.63}, "resistance": {"price": 4030.75},
              "levels": [3973.63, 4030.75, 4018.80, 4251.95, 3810.0]}]
    sec = ("## Levels in Focus\n"
           "GOLD support is 1.1% below; the monthly CPR's bottom line (BC at "
           "4,251.95) is nearly 6% above, and a break opens the monthly S1 at 3,810.\n")
    assert scan_confluence_fabrication(sec, focus) == []


def test_widened_levels_set_still_flags_an_invented_level():
    """Teeth: widening the grounded set must not turn the guard into decoration.
    A level that is in no computed zone still fires."""
    focus = [{"asset": "GOLD", "price": 4018.80,
              "support": {"price": 3973.63}, "resistance": {"price": 4030.75},
              "levels": [3973.63, 4030.75, 4018.80, 4251.95, 3810.0]}]
    sec = "## Levels in Focus\nGOLD resistance now sits at 4,600.00.\n"
    out = scan_confluence_fabrication(sec, focus)
    assert out and out[0]["type"] == "fabricated_level"


def test_all_grounded_levels_pulls_cpr_members_out_of_zones():
    """confluence_block must hand the scanner the individual zone members, not
    just the clustered zone prices — CPR pivots live in `members`."""
    from morning_engine.confluence_block import _all_grounded_levels
    payload = {"price": 100.0, "all": [
        {"price": 95.5, "members": [{"code": "S1", "price": 95.0},
                                    {"code": "BB_L", "price": 96.0}]},
        {"price": 104.0, "members": [{"code": "R1", "price": 104.0}]},
    ]}
    assert sorted(_all_grounded_levels(payload)) == [95.0, 95.5, 96.0, 104.0, 104.0]


# --- _confluence_tripwire integration tests ----------------------------------

import logging
from morning_engine.engine import _confluence_tripwire


def test_tripwire_logs_on_fabricated_level(caplog, notifications):
    lif = "## Levels in Focus\nBTC support now sits at 58,500."
    with caplog.at_level(logging.ERROR):
        _confluence_tripwire(lif, FOCUS, "general report")
    assert "confluence fabrication" in caplog.text


def test_tripwire_silent_when_clean(caplog):
    lif = "## Levels in Focus\nBTC support at 62,020."
    with caplog.at_level(logging.ERROR):
        _confluence_tripwire(lif, FOCUS, "general report")
    assert "confluence fabrication" not in caplog.text


_FOCUS = [{"asset": "BTC", "price": 59834, "support": {"price": 59130}, "resistance": {"price": 60013}},
          {"asset": "GOLD", "price": 4127, "support": {"price": 4038}, "resistance": {"price": 4143}}]

# Report whose LiF section is a pure markdown table (no added prose lines).
# SPX 7,365 in The Story and CPR 66,008/59,503 in Scenarios are outside LiF
# and must NOT be flagged.
_REPORT = (
    "## The Story\n"
    "Gold sits at 19% of range while SPX fell to 7,365 below its monthly pivot range.\n\n"
    "## Scenarios\n"
    "BTC could target the 66,008 CPR S2 or the 59,503 CPR S3 support.\n\n"
    "## LEVELS IN FOCUS\n"
    "| Asset | Price | Nearest support | Nearest resistance |\n"
    "|---|---|---|---|\n"
    "| BTC | 59,834 | 59,130 (1.2% below, faint) | 60,013 (0.3% above, faint) |\n"
    "| GOLD | 4,127 | 4,038 (2.1% below, medium) | 4,143 (0.4% above, strong) |\n"
)


def test_tripwire_ignores_levels_outside_the_lif_section(caplog):
    # SPX 7,365 (Story) and CPR 66,008/59,503 (Scenarios) are grounded elsewhere — must NOT flag
    with caplog.at_level(logging.ERROR):
        _confluence_tripwire(_REPORT, _FOCUS, "general report")
    assert "confluence fabrication" not in caplog.text


def test_tripwire_flags_invented_level_in_table(caplog, notifications):
    """Altering a table cell in the LiF section to a bogus value MUST flag."""
    bad = _REPORT.replace("60,013 (0.3% above, faint)", "63,500 (bogus)")
    with caplog.at_level(logging.ERROR):
        _confluence_tripwire(bad, _FOCUS, "general report")
    assert "confluence fabrication" in caplog.text
