"""scan_advice must catch the afternoon report's actual
violation idiom — bare imperative portfolio instructions — not just the
"you should" framing it originally covered.

Positives are verified violating sentences quoted verbatim from a published
afternoon report.
Negatives are real `general` report bodies (report has been clean for
months and is the flagship free-tier product) plus synthetic descriptive
sentences that share vocabulary with the banned constructions but are not
advice, mirroring the calibration warning in the original report and the module
docstring.
"""
from carrac.grounding.advice_scan import scan_advice


# --- Positives: verified violations from a published afternoon report -------------

def test_flags_reduce_or_hedge_imperative():
    assert scan_advice(
        "Reduce or hedge ETH longs if this level fails on a closing basis."
    )


def test_flags_do_not_chase():
    assert scan_advice(
        "Overbought condition - do not chase; wait for a pullback toward "
        "$6.32-$6.38 CPR zone."
    )


def test_flags_avoid_carry_strategies():
    assert scan_advice("Avoid ETH carry strategies.")


def test_flags_consider_trimming():
    assert scan_advice("Consider trimming longs or tightening stops.")


def test_flags_if_you_are_long():
    assert scan_advice("If you are long, this is the first exit zone.")


def test_flags_do_not_add_exposure():
    assert scan_advice("Do not add alt exposure until this confirms.")


def test_flags_you_should_buy_now():
    assert scan_advice("You should buy now.")


# --- Negatives: descriptive prose that must NOT match -----------------------

def test_ignores_descriptive_past_tense_reduce():
    clean = ("Institutional flows reduced exposure into the close, and funds "
              "continue to look to reduce risk heading into the print.")
    assert scan_advice(clean) == []


def test_ignores_dealer_hedging_prose():
    clean = ("GEX measures how much options dealers need to hedge as price "
              "moves; dealers hedge in the same direction as the move when "
              "gamma is negative.")
    assert scan_advice(clean) == []


def test_ignores_rotation_theme_prose():
    clean = ("The commodity rotation thesis is not theoretical today; it is "
              "printing live in the data. Capital continues to rotate into "
              "value from growth.")
    assert scan_advice(clean) == []


def test_ignores_do_not_confirm():
    clean = "Markets do not confirm the breakout without a volume expansion."
    assert scan_advice(clean) == []


def test_ignores_avoided_as_past_tense():
    clean = "Volatility avoided the highs printed earlier in the session."
    assert scan_advice(clean) == []


def test_ignores_bare_market_vocabulary():
    clean = ("Sellers absorbed the move; oversold RSI, heavy sell-side flow, "
              "buyers stepped in at support. Overbought conditions persist.")
    assert scan_advice(clean) == []


def test_ignores_range_position_and_percentile_language():
    clean = ("SOYBEANS sits at the 94th percentile of its range position, "
              "with resistance well above and no exit signal in the data.")
    assert scan_advice(clean) == []


# --- Negative corpus: real `general` report bodies (12 most recent, production) --

_GENERAL_REPORT_NEGATIVES = [
    # A representative slice of real published `general` report prose
    # (2026-07 to 2026-08) exercising the exact vocabulary the new patterns
    # key on — reduce/hedge/rotate/position/exposure/carry/consider — in
    # purely descriptive, third-person framing. None of these are advice.
    "The commodity rotation thesis is not theoretical today; it is printing "
    "live in the data. All three carry bullish EMA ribbons.",
    "BTC's futures basis at +3.90% APR sits fractionally above the 3.62% Fed "
    "funds rate — barely positive carry. GEX is the net options exposure "
    "that dealers must hedge, with the flip point at $64,691.",
    "A renewal or softer-than-feared replacement would reduce agricultural "
    "commodity uncertainty and support equities.",
    "OIL closing back below $75 would deflate the energy bid and reduce "
    "commodity rotation momentum.",
    "The 25-delta skew at +9.7% means put options are more expensive than "
    "calls, reflecting persistent defensive positioning.",
    "A soft PCE print would reduce hawkish Fed overhang, pressure DXY, and "
    "potentially reopen the commodity/crypto bid.",
    "When dealers are net short gamma, they hedge in the same direction as "
    "the market move, which amplifies it.",
    "Equities benefit from the mirror image: lower energy costs reduce input "
    "inflation, which softens the Fed's hawkish posture at the margin.",
]


def test_no_false_positives_on_general_report_negatives():
    fp_count = sum(len(scan_advice(t)) for t in _GENERAL_REPORT_NEGATIVES)
    assert fp_count == 0, (
        "New advice patterns false-positived on real general-report prose "
        f"({fp_count} hits) — general has been clean for months and must "
        "stay clean."
    )


# --- Second pass: the PERSONALIZED-report idiom --------------
#
# Different shape again: a "[HIGH]"-style priority tag sits between the bullet
# and the verb, and the instruction often follows a colon mid-line. Positives
# below are the shape of real stored personal_report_sections content.
#
# The line these draw: instructing an action ON A POSITION is advice;
# directing ATTENTION is the approved observational register that the
# rewritten prompts are supposed to produce. Monitor/Watch/Review must stay
# clean or the guard would fight the very register it exists to protect.

def test_flags_protect_position_with_priority_tag():
    assert scan_advice(
        "- [HIGH] Protect ETH long x3: Trail SL if TC CPR 29% range breaks down."
    )


def test_flags_trail_stop_instruction():
    assert scan_advice("Trail SL if the level fails.")
    assert scan_advice("Trail your stop under the prior swing low.")


def test_flags_recommended_partial_exit():
    assert scan_advice(
        "- [HIGH] Review SPX short (id 34712/9352): low risk_reward 0.33 "
        "flags partial exit if RSI >35."
    )


def test_ignores_monitor_and_watch_the_approved_register():
    """Directing attention is what the rewritten prompts ASK for."""
    assert not scan_advice(
        "- [HIGH] Monitor BC CPR level for bounce invalidation; volume ratio above 1.22."
    )
    assert not scan_advice(
        "Watch for a close below $1,830 to flip the structure bearish."
    )


def test_ignores_trailing_stop_as_a_noun():
    """'A trailing stop order' is ordinary commentary, not an instruction."""
    assert not scan_advice(
        "A trailing stop order sat just under the prior swing low."
    )


def test_ignores_descriptive_partial_exit():
    assert not scan_advice(
        "Institutions took partial exits last week as open interest fell."
    )


def test_ignores_hedging_flows_prose():
    assert not scan_advice(
        "Hedging flows dominated the session as dealers rebalanced exposure."
    )
