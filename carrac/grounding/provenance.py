"""Per-fact provenance: map a report's numbers back to the briefing block
that supplied them (step 3 of the grounding sequence).

Deliberately deterministic — no LLM. Every heading-delimited block of the
persisted ``reports.source_briefing`` is indexed by the numbers it contains;
every number in the report is then looked up. A number with no source block is
an *orphan*: either derived by the model (a percentage it computed) or invented.

Two properties this preserves on purpose:

* **No citation clutter.** Provenance lives in this index, computed after the
  fact from stored text. The report prose is untouched — readability is one of
  the reports' real strengths and footnotes would cost it. This mirrors the
  calendar/Levels-in-Focus pattern, which gets provenance by making the
  artifact itself verbatim rather than by annotating sentences.
* **Precision-tolerant matching.** The briefing carries full precision
  (``62,425.48``) while the report rounds (``$62,425``). String matching
  under-reports provenance badly — measured at 91.4% -> 8.6% orphans on report
  373 purely from formatting. A report value shown with D decimals is grounded
  by a briefing value B when ``round(B, D)`` equals it.

NOT wired to a tripwire. The orphan rate is measured on very few persisted
briefings (n=1 at the time of writing), so an alarm on it would be noise.
Measure across a few weeks of briefings first,
then decide whether it earns one.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Set, Tuple

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_HEAD = re.compile(r"^(#{1,3})\s*(.+)$", re.M)

# Numbers that are units or ordinals rather than quantities worth sourcing.
_NOT_A_FACT = re.compile(r"\s*(?:%|percent(?:ile|age)?\b|pct\b|(?:st|nd|rd|th)\b)",
                         re.IGNORECASE)

# Report sections rendered deterministically by code AFTER generation, from
# live queries that never entered the briefing. Their numbers are not model
# claims, so an orphan there means "not retained", not "possibly invented".
# Kept as a visible default rather than hidden in the matcher — a new
# deterministic section that is not listed here will show up as orphans, which
# is the safe direction to fail.
DETERMINISTIC_SECTIONS = (
    "Market Snapshot",
    "Spotlight — Levels & Structure",
    "COT Positioning",
    "Whale Watch",
    "Bitcoin Network Fundamentals",
    "Market Pulse",
    "Cross-Asset Heat",
    "Correlation Shifts",
)

# Marks the block appended to source_briefing after generation. Everything
# below this line was NOT in the model's prompt — it is retained evidence so a
# claim can be checked later. Loud on purpose: source_briefing otherwise reads
# as "exactly what the model saw", and a silent addition would make it lie.
RETAINED_HEADER = (
    "=== RETAINED SOURCES — CAPTURED AFTER GENERATION, NOT SENT TO THE MODEL ==="
)


def split_retained(source_briefing: str) -> Tuple[str, str]:
    """Split a stored briefing into (prompt_text, retained_text).

    Use prompt_text whenever you need what the model actually saw — e.g.
    re-running the Haiku verifier against a stored report.
    """
    if not source_briefing:
        return "", ""
    parts = source_briefing.split(RETAINED_HEADER, 1)
    if len(parts) == 1:
        return source_briefing, ""
    return parts[0].rstrip(), (RETAINED_HEADER + parts[1])


def render_retained_sources(level_sets: Dict[str, Iterable[float]]) -> str:
    """Render grounded level sets as a retained-sources block, or '' if empty.

    ``level_sets`` maps asset -> every level the confluence engine computed for
    it. The rendered Levels-in-Focus table keeps only the nearest support and
    resistance, so without this the CPR pivots, bands, EMAs and swing points a
    report legitimately cites leave no trace — which is why an earlier GEX
    claim could not be diagnosed after the fact.
    """
    rows = []
    for asset in sorted(level_sets or {}):
        vals = sorted({round(float(v), 8) for v in (level_sets[asset] or [])})
        if not vals:
            continue
        rows.append(f"{asset}: " + ", ".join(f"{v:,.8g}" for v in vals))
    if not rows:
        return ""
    return "\n".join([
        RETAINED_HEADER,
        "## Computed confluence levels (every level behind each asset's zones)",
        *rows,
    ])


def _blocks(text: str) -> List[Tuple[int, int, str]]:
    heads = [(m.start(), m.group(2).strip()) for m in _HEAD.finditer(text or "")]
    out: List[Tuple[int, int, str]] = []
    if heads and heads[0][0] > 0:
        out.append((0, heads[0][0], "(preamble)"))
    for i, (pos, label) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        out.append((pos, end, label))
    return out


def _parse(tok: str):
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def index_source(source_text: str, max_decimals: int = 4) -> Dict[Tuple[float, int], Set[str]]:
    """(rounded value, decimals) -> block labels that contain it."""
    idx: Dict[Tuple[float, int], Set[str]] = defaultdict(set)
    for start, end, label in _blocks(source_text):
        for m in _NUM.finditer(source_text[start:end]):
            v = _parse(m.group(0))
            if v is None:
                continue
            for d in range(0, max_decimals + 1):
                idx[(round(v, d), d)].add(label)
    return idx


def _report_facts(report_text: str):
    """(token, value, decimals, section, context) for each sourceable number.

    ``section`` is the heading path, outermost first ("Spotlight — Levels &
    Structure > WHEAT — $634.75"). Nesting matters: ta_cards.py renders one
    ``## Spotlight — Levels & Structure`` block with a ``###`` sub-heading per
    asset, and those sub-sections are code output too. Tracking only the last
    heading would report every one of their numbers as a model claim.
    """
    path: List[Tuple[int, str]] = []
    for line in (report_text or "").splitlines():
        s = line.strip()
        m = _HEAD.match(s)
        if m:
            level = len(m.group(1))
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, m.group(2).strip()))
            continue
        section = " > ".join(lbl for _, lbl in path) or "(top)"
        for m in _NUM.finditer(s):
            if _NOT_A_FACT.match(s, m.end()):
                continue
            tok = m.group(0)
            v = _parse(tok)
            if v is None:
                continue
            dec = len(tok.split(".")[1]) if "." in tok else 0
            yield tok, v, dec, section, s[max(0, m.start() - 70):m.end() + 30]


def check_provenance(report_text: str, source_text: str,
                     skip_sections: Iterable[str] = DETERMINISTIC_SECTIONS) -> dict:
    """Map every number in *report_text* to the briefing block that supplied it.

    Returns {checked, grounded, orphans, by_section}. ``orphans`` lists facts
    with no source block — the only entries worth a human's attention. A fact
    is counted but not reported when ANY heading on its path starts with one of
    *skip_sections*, so a code-rendered block's per-asset sub-sections are
    covered by naming the parent once (see DETERMINISTIC_SECTIONS).
    """
    idx = index_source(source_text)
    skip = tuple(skip_sections or ())

    def _is_deterministic(path: str) -> bool:
        return any(part.strip().startswith(skip) for part in path.split(" > ")) if skip else False
    orphans: List[dict] = []
    by_section: Dict[str, dict] = defaultdict(lambda: {"grounded": 0, "orphan": 0})
    checked = grounded = 0
    for tok, v, dec, section, ctx in _report_facts(report_text):
        checked += 1
        labels = idx.get((round(v, dec), dec))
        stats = by_section[section]
        if labels:
            grounded += 1
            stats["grounded"] += 1
            continue
        stats["orphan"] += 1
        if not _is_deterministic(section):
            orphans.append({"value": tok, "section": section, "context": ctx.strip()})
    return {"checked": checked, "grounded": grounded,
            "orphans": orphans, "by_section": dict(by_section)}


def source_of(value: str, source_text: str) -> List[str]:
    """Block labels that could have supplied *value*. Diagnosis helper: given a
    suspect claim from a published report, name where it came from — or return
    [] to show it came from nowhere."""
    v = _parse(value)
    if v is None:
        return []
    dec = len(value.split(".")[1]) if "." in value else 0
    return sorted(index_source(source_text).get((round(v, dec), dec), set()))
