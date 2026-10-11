"""Afternoon-report grounding tripwire.

The afternoon delta report is free-text LLM prose narrating "what changed
since this morning" (Session Summary, Derivatives Delta / Breadth note,
Treasuries prose). Such "since morning" claims can be flatly wrong. Known
shapes:

  08-03  regime "Euphoric Melt-Up 57%" vs the same-day morning's actual
         "Vol Compression / Coiling 79%" (the afternoon's own Session
         Summary contradicted its own label too).
  08-05  breadth "24% oversold — unchanged from the morning read" vs the
         morning report's actual 7%.
  08-07  header spread +0.45 vs body prose "ticked to +0.44 (from +0.45) —
         compressing" in the SAME document.

The STRUCTURED delta object (build_delta's regime.was/now, fear_greed) is
grounded in morning_engine/afternoon_delta.py. This module checks the
FREE-TEXT narrative, which — per the 08-05 and 08-07 cases above — has the
real numbers available (the stored morning report, or the report's own
header) and can still get them wrong.

Two regex-based checks, same idiom as the rotation-quadrant
tripwire (carrac/grounding/hallucination_scan.py::scan_rotation_quadrant_
mismatch) — a best-effort net against known report shapes, not a parser:

  check_spread_self_consistency(content)
      Intra-document (catches the 08-07 shape). The "Macro facts" header
      line states the CURRENT 10Y-2Y spread deterministically (computed
      from FRED at generation time, not LLM prose — see
      morning_engine/data_assembly.py). If body prose restates a different
      "current" value for the same metric, that is a same-document
      self-contradiction. No morning report needed.

  check_breadth_continuity(afternoon_content, morning_content)
      Cross-document (catches the 08-05 shape). When the afternoon prose
      explicitly claims breadth oversold% is unchanged/same as the morning
      read, the cited number must actually match what the morning report
      stated.

The 08-03 regime case is deliberately NOT covered here: the two "regime"
numbers involved (a derivatives-regime playbook label vs the quant HMM
regime) come from two different taxonomies (see quant/engine/derivatives_
regime.py vs the HMM regime in data_assembly.py) and a robust text-only
disambiguation is out of reach for a save-time regex net.

Both checks are non-fatal, log.error + error-tracker capture — same asymmetry as
carrac.compliance.deribit_gate.alert_if_barred_prose: notify, never
withhold a save.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger('carrac.reports.afternoon_grounding')

# "10Y 4.63% / 2Y 4.18% (spread +0.45)" — the deterministic macro-facts
# header line built in morning_engine/data_assembly.py, not LLM prose.
_HEADER_SPREAD_RE = re.compile(r'\(spread\s*([+-]?\d+(?:\.\d+)?)\)')

# Body-prose restatements of the same metric, e.g. "The 10Y-2Y spread ticked
# to +0.44% (from +0.45%)" or "the 10Y-2Y yield spread sits at +0.44%".
_BODY_SPREAD_RE = re.compile(
    r'10Y-2Y\s+(?:yield\s+)?spread\s+(?:sits at|reads|ticked to|stands at|is at|is now)\s*'
    r'([+-]?\d+(?:\.\d+)?)%?',
    re.IGNORECASE,
)

# "7% of the tracked universe in oversold territory" / "24% oversold" — both
# the morning and afternoon prompts (morning_engine/prompts.py) instruct the
# model to cite the breadth oversold% this way; deliberately loose enough to
# match either phrasing.
_OVERSOLD_PCT_RE = re.compile(
    r'(\d+(?:\.\d+)?)%\s*(?:of (?:the )?tracked universe )?(?:in )?oversold',
    re.IGNORECASE,
)

_UNCHANGED_FROM_MORNING_RE = re.compile(
    r'unchanged (?:from|since) the morning'
    r'|same as the morning'
    r'|no change (?:from|since) (?:the )?morning',
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')

# Tolerances: both sides are rounded to 2dp by the generator (the header is
# computed, not LLM prose), so this only needs to absorb float noise — a
# 0.01-level gap (e.g. the real 08-07 case: header +0.45 vs body +0.44) is
# already a genuine mismatch, not rounding.
_SPREAD_TOLERANCE = 0.005
_BREADTH_TOLERANCE_PCT = 0.5


def _capture(message: str) -> None:
    """Send one message to Sentry. Seam so the alarm is testable."""
    import sentry_sdk
    sentry_sdk.capture_message(message, level="error")


def _alarm(message: str) -> None:
    logger.error("[grounding] %s", message)
    try:
        _capture(message)
    except Exception:  # noqa: BLE001
        # logger.error above already fired — this is not a silent loss.
        logger.warning("afternoon grounding alarm: Bugsink capture failed", exc_info=True)


def check_spread_self_consistency(content: str) -> list[dict]:
    """Compare the header's deterministic 10Y-2Y spread against any body
    prose restatement of the same metric within the SAME report.

    Returns a list of findings (empty when clean, or when the header or no
    body restatement is present — this only fires when both sides exist and
    disagree).
    """
    if not content:
        return []
    header_match = _HEADER_SPREAD_RE.search(content)
    if not header_match:
        return []
    try:
        header_value = float(header_match.group(1))
    except ValueError:
        return []

    findings = []
    for body_match in _BODY_SPREAD_RE.finditer(content):
        try:
            body_value = float(body_match.group(1))
        except ValueError:
            continue
        if abs(body_value - header_value) > _SPREAD_TOLERANCE:
            findings.append({
                'type': 'spread_self_contradiction',
                'header_value': header_value,
                'body_value': body_value,
                'body_excerpt': content[max(0, body_match.start() - 40):body_match.end() + 20].strip(),
            })
    return findings


def check_breadth_continuity(afternoon_content: str, morning_content: str) -> list[dict]:
    """When the afternoon prose claims breadth is unchanged from the
    morning, the oversold% it cites must match what the morning report
    actually stated.

    Returns a list of findings (empty when clean, when no "unchanged" claim
    is made, or when either side lacks a parseable %).
    """
    if not afternoon_content or not morning_content:
        return []

    morning_match = _OVERSOLD_PCT_RE.search(morning_content)
    if not morning_match:
        return []
    try:
        morning_value = float(morning_match.group(1))
    except ValueError:
        return []

    findings = []
    for sentence in _SENTENCE_SPLIT_RE.split(afternoon_content):
        if not _UNCHANGED_FROM_MORNING_RE.search(sentence):
            continue
        afternoon_match = _OVERSOLD_PCT_RE.search(sentence)
        if not afternoon_match:
            continue
        try:
            afternoon_value = float(afternoon_match.group(1))
        except ValueError:
            continue
        if abs(afternoon_value - morning_value) > _BREADTH_TOLERANCE_PCT:
            findings.append({
                'type': 'breadth_continuity_mismatch',
                'afternoon_value': afternoon_value,
                'morning_value': morning_value,
                'sentence': sentence.strip(),
            })
    return findings


def check_afternoon_grounding(afternoon_content: str, morning_content: str | None) -> list[dict]:
    """Run both tripwires against a newly-saved afternoon report.

    Never raises — same asymmetry as alert_if_barred_prose: notify, never
    withhold a save. Fires one error-tracker alarm naming every finding when any
    check trips; silent on a clean report (a guard nobody
    reads becomes noise).
    """
    try:
        findings = list(check_spread_self_consistency(afternoon_content))
        if morning_content:
            findings.extend(check_breadth_continuity(afternoon_content, morning_content))
        if findings:
            detail = '; '.join(f"{f['type']}: {f}" for f in findings)
            _alarm(
                f"Afternoon report grounding mismatch on save "
                f"({len(findings)} finding(s)): {detail}"
            )
        return findings
    except Exception:  # noqa: BLE001
        logger.warning("afternoon grounding check failed (non-fatal)", exc_info=True)
        return []


def check_general_grounding(content: str) -> list[dict]:
    """Run the spread self-consistency tripwire against a newly-saved
    'general' report. A general report can carry the same header-vs-body
    spread contradiction (header "(spread +0.45)", body "The 10Y-2Y yield
    spread sits at +0.44%") that check_afternoon_grounding catches in the
    AFTERNOON report; save_general_report() must call this to catch it.

    Reuses check_spread_self_consistency() unmodified — same function the
    afternoon check calls, not a second copy of the rule.

    No breadth-continuity check here: that check compares the afternoon
    report against "the morning read" (a different document); the general
    report IS the morning read, so there is nothing to diff it against.

    Same asymmetry as check_afternoon_grounding: never raises, non-fatal for
    the save, one error-tracker alarm naming every finding, silent on a clean
    report.
    """
    try:
        findings = list(check_spread_self_consistency(content))
        if findings:
            detail = '; '.join(f"{f['type']}: {f}" for f in findings)
            _alarm(
                f"General report grounding mismatch on save "
                f"({len(findings)} finding(s)): {detail}"
            )
        return findings
    except Exception:  # noqa: BLE001
        logger.warning("general grounding check failed (non-fatal)", exc_info=True)
        return []
