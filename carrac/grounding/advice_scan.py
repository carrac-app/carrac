"""Post-generation advice-phrase tripwire.

The non-advice posture was enforced at prompt level only.
Deliberately HIGH-precision: BANNED_ADVICE_PHRASES contains bare
'buy'/'sell', which would false-positive on 'buyers'/'sell-off'/'oversold'
— so this scans for advice *constructions*, not bare verbs. Non-blocking:
wired as a log-only tripwire like _news_tripwire.

The afternoon report's actual violation idiom was NOT
"you should" — it was the bare imperative ("Reduce or hedge ETH longs",
"Avoid ETH carry strategies", "Consider trimming longs", "If you are
long, this is the first exit zone"). scan_advice returned [] for every
one of the verified violating sentences before this extension. The four
patterns added below cover that idiom while staying HIGH-precision:

  1. Sentence/line-initial bare imperative verb (Reduce/Hedge/Trim/Avoid/
     Chase/Exit/Rotate) + a nearby portfolio-object noun. The anchor on
     sentence-start (start-of-string, after ". "/"! "/"? "/newline, or a
     bullet marker) is what separates a COMMAND ("Reduce or hedge ETH
     longs…") from descriptive third-person prose that uses the same verb
     mid-sentence in past/continuous tense or after a subject ("traders
     reduced exposure", "dealers hedge their books", "capital continues
     to rotate into value") — none of those start a sentence with the
     bare verb form, and the object-noun requirement additionally excludes
     ordinary sentence-initial nouns/verbs with no portfolio object nearby
     ("Rotation is the week's dominant theme" has no "longs/shorts/
     exposure/…" within range). Verified against 60 real `general` report
     bodies (2026-06 through 2026-08, `general` has been clean for
     months) — zero false positives, including report text that uses
     "reduce"/"hedge"/"rotate" liberally in descriptive dealer-hedging and
     macro-catalyst prose.
  2. "do not <verb>" restricted to a whitelist of trading-action verbs
     (chase/buy/sell/add/hold/rotate/exit/enter/fade/average/hedge/trim/
     reduce) so it can't fire on a descriptive negative like "markets do
     not confirm the breakout" (verb "confirm" is not in the whitelist).
  3. "consider <verb>ing" — always reader-directed suggestion framing; no
     natural descriptive-prose use of this exact construction turned up
     in the 60-report corpus.
  4. "if you are/you're long/short" — direct second-person address to the
     reader's own position, unambiguous advice framing.
"""
import re

_ADVICE_PATTERNS = [
    re.compile(r"\byou should\b", re.I),
    re.compile(r"\b(?:I|we) recommend\b", re.I),
    re.compile(r"\bI would (?:buy|sell|long|short)\b", re.I),
    re.compile(r"\b(?:buy|sell) (?:now|here|the dip|this dip)\b", re.I),
    re.compile(r"\b(?:go|get) (?:long|short) (?:now|here)\b", re.I),
    re.compile(r"\byou (?:need|ought) to (?:buy|sell)\b", re.I),
    # Additions — see module docstring for calibration notes.
    re.compile(
        r"(?:^|(?<=[.!?]\s)|(?<=\n)|(?<=- )|(?<=\* )|(?<=• ))"
        r"(?:Reduce|Hedge|Trim|Avoid|Chase|Exit|Rotate)\b"
        r"[^.\n]{0,40}?\b(?:longs?|shorts?|exposure|positions?|stops?|"
        r"strateg\w*|holdings?|carry)\b",
        re.MULTILINE | re.I,
    ),
    re.compile(
        r"\bdo not (?:chase|buy|sell|add|hold|rotate|exit|enter|fade|"
        r"average|hedge|trim|reduce)\b", re.I,
    ),
    re.compile(r"\bconsider \w+ing\b", re.I),
    re.compile(r"\bif you(?:'re| are) (?:long|short)\b", re.I),
    # Second pass — the PERSONALIZED-report idiom, which is a
    # different shape again and which the four patterns above all miss. Real
    # examples from stored personal_report_sections:
    #   "- [HIGH] Protect ETH long x3: Trail SL if TC CPR range breaks down."
    #   "- [HIGH] Review SPX short (id …): low risk_reward flags partial exit
    #     if RSI >35."
    # Two structural differences from the afternoon idiom: a "[HIGH]"-style
    # priority tag sits between the bullet and the verb (so the sentence-start
    # lookbehinds above never reach it), and the instruction often follows a
    # colon mid-line rather than opening the sentence.
    #
    # THE LINE THIS DRAWS: instructing an action ON A POSITION is advice;
    # directing ATTENTION is the approved observational register. "Monitor the
    # BC CPR level", "Watch for a close below X" must stay clean — that is
    # exactly what section 6 of the afternoon prompt and the rewritten
    # personalized sections are supposed to produce. So Monitor/Watch/Review
    # are deliberately NOT listed: they aim the reader's eyes, not their book.
    # Only verbs that act on a holding are listed.
    re.compile(
        r"(?:^|\n)\s*(?:[-*•]\s*)?(?:\[[A-Z]+\]\s*)?"
        r"(?:Protect|Trail|Trim|Reduce|Hedge|Unwind|Close out)\b"
        r"[^.\n]{0,60}?\b(?:longs?|shorts?|positions?|exposure|stops?|SL)\b",
        re.MULTILINE | re.I,
    ),
    # "Trail SL"/"trail the stop" — an instruction to move a stop. Imperative
    # form ONLY: the "-ing" variant is excluded because "a trailing stop order"
    # is an ordinary noun phrase in descriptive commentary, not advice.
    re.compile(r"\btrail (?:the |your )?(?:SL|stop)\b", re.I),
    # "partial exit"/"scale out" as a recommended action. Requires an
    # instruction-framing lead-in ("flags"/"suggests"/"consider"/"take") so a
    # genuinely descriptive "institutions took partial exits last week" style
    # sentence is not swept up.
    re.compile(
        r"\b(?:flags?|suggests?|consider|take|taking)\b[^.\n]{0,30}?"
        r"\b(?:partial exit|scale out|scaling out)\b", re.I,
    ),
    # The pattern at the top of this list needs an object noun
    # within 40 chars AFTER the verb, which is why "Reduce or hedge" was caught
    # twice out of four live instances and missed twice:
    #
    #   caught  "…the $1,617 S1. Reduce or hedge ETH longs if this level fails"
    #   MISSED  "…may be near-term exhausted. Reduce or hedge rather than chase."
    #            -> no object noun at all
    #   MISSED  "- **Altcoin exposure — Reduce or hedge:** The breadth of…"
    #            -> object noun sits BEFORE the verb, and the bullet lookbehind
    #               needs the verb immediately after "- "
    #
    # Both shipped in published afternoon reports. The compound imperative is
    # unambiguous on its own — no object needed — so it is matched as a fixed
    # phrase. Bare "reduce" is deliberately NOT added: "the Fed reduced rates",
    # "implied volatility reduced sharply" and "breadth reduced to 34%" are all
    # ordinary commentary, and over-matching turns this into noise.
    re.compile(
        r"\b(?:reduce|trim|cut)\s+or\s+(?:hedge|exit|trim)\b"
        r"|\bhedge\s+or\s+(?:reduce|trim|cut)\b", re.I,
    ),
    # "Now is the time to <act>" — directs the book, not the eyes.
    re.compile(
        r"\bnow is the time to\s+\w+"
        r"|\btime to (?:buy|sell|rotate|trim|add|hedge|exit|reduce)\b", re.I,
    ),
]


def scan_advice(text) -> list:
    """Return matched advice phrases in *text* (empty list = clean)."""
    if not text:
        return []
    return [m.group(0) for pat in _ADVICE_PATTERNS for m in pat.finditer(text)]


# prescriptive-scenario tripwire. The general (free-tier) report's
# "## What Would Change the Picture" section is observational only (brand rule
# product rule): no entry/trigger price levels, no invalidation levels,
# no "Scenario A/B" trade legs, no weight-of-evidence directional lean. These
# patterns catch a slip back into that framing so the prompt can be tightened.
#
# ADVISORY ONLY — kept OUT of the blocking hallucination gate (has_high /
# _publish_gate_ok in hallucination_scan.py); wired as a log-only tripwire like
# scan_advice, so a stray word can never take a report offline. High-precision:
# "Trigger:" needs the colon (avoids "triggered"/"triggers"); "Scenario [AB]"
# is case-sensitive (the capitalised label, not lowercase prose like "in this
# scenario a break…"); the lean is the full phrase, not bare "evidence".
_PRESCRIPTIVE_SCENARIO_PATTERNS = [
    re.compile(r"\bTrigger:\s", re.I),
    re.compile(r"\bInvalidation\b", re.I),
    re.compile(r"\bScenario [AB]\b"),
    re.compile(r"weight of evidence leans", re.I),
]


def scan_prescriptive_scenario(text) -> list:
    """Return matched prescriptive-scenario phrases in *text* (empty = clean).

    Same shape/severity tier as :func:`scan_advice` — a plain list of matched
    strings, consumed by a non-blocking tripwire (never the HIGH publish gate).
    """
    if not text:
        return []
    return [m.group(0) for pat in _PRESCRIPTIVE_SCENARIO_PATTERNS for m in pat.finditer(text)]
