"""Regex tripwire catching the training-era leak class that fired in
a Grok voice briefing ('Fed cut 50bp to 4.75-5%, S&P 5,980,
CPI 2.4%' — all Sept 2024 values).

Zero LLM cost. Runs in <5ms. Fires BEFORE publish and blocks HIGH-severity
findings. Logs MEDIUM + LOW for observability.

Patterns are cross-checked against the ground-truth facts dict where
possible: e.g. a 'rate cut 50bp' claim is HIGH only if no such cut
occurred on the current date per facts['last_fomc'] + facts['today'].
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger("carrac.grounding.scan")

# Warn once at import if hallucination scanning is disabled so an
# accidental GROUNDING_ENABLED=0 is visible in logs. Does NOT change behavior.
if os.environ.get("GROUNDING_ENABLED", "1") == "0":
    log.warning(
        "GROUNDING_ENABLED=0 — hallucination scan is DISABLED. "
        "All scan() calls will return [] with no protection. "
        "Set GROUNDING_ENABLED=1 to re-enable."
    )


class Severity(str, Enum):
    HIGH = "HIGH"        # block publish
    MEDIUM = "MEDIUM"    # log + alert
    LOW = "LOW"          # observability only


@dataclass
class Finding:
    pattern_name: str
    severity: Severity
    match_text: str
    span: tuple
    context: str
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern": self.pattern_name,
            "severity": self.severity.value,
            "match": self.match_text,
            "span": list(self.span),
            "context": self.context[:240],
            "reason": self.reason,
        }


# ---------------------------------------------------------------------
# Patterns — ordered most-specific first so overlapping matches resolve
# to the more-serious finding.
# ---------------------------------------------------------------------
_PATTERNS: List[tuple] = [
    # Fabricated recent-Fed-action claims — the core failure mode
    (
        "fabricated_fed_event",
        re.compile(r"\b(today|yesterday|this\s+morning|overnight|just)\b[^.]{0,60}\b(fed|fomc|federal\s+reserve|powell)\b[^.]{0,60}\b(cut|hike|slash|raise|lower|trim|reduce|ease)", re.IGNORECASE),
        Severity.HIGH,
        "Claim of a recent Fed action — must be verified against facts['last_fomc'] and facts['today'].",
    ),
    (
        "fed_surprise",
        re.compile(r"\bfed\s+(surprise[ds]?|shock|stunn|unexpected)[^.]{0,60}\b(cut|hike|slash)", re.IGNORECASE),
        Severity.HIGH,
        "Claim of a surprise Fed action — training-era news headline pattern.",
    ),
    (
        "rate_cut_quantum",
        re.compile(r"\b(cut|hike|slash|lower|raise)[^.]{0,30}(\d{1,3})\s*(basis\s*points?|bps?|b\.p\.)\b", re.IGNORECASE),
        Severity.MEDIUM,
        "Numeric rate-action claim — must match a real FOMC decision in facts.",
    ),
    # Sept-2024 signature rate ranges.
    # Allow bare "5" or "5%" after the dash ("4.75-5%").
    (
        "sept_2024_rate_range",
        re.compile(r"\b4\.(?:50|75)\s*[-–—]\s*(?:5(?:\.0{1,2})?|4\.75|4\.50)(?:\s*%|\b)", re.IGNORECASE),
        Severity.HIGH,
        "Matches the Sept-2024 post-cut Fed funds band (4.50–5.00%). Training-data bleed signature.",
    ),
    (
        "pre_cut_2024_rate_range",
        re.compile(r"\b5\.(?:25|50)\s*[-–—]\s*5\.(?:50|25)(?:\s*%|\b)", re.IGNORECASE),
        Severity.HIGH,
        "Matches the pre-Sept-2024 Fed funds band (5.25–5.50%). Training-data bleed signature.",
    ),
    # Historic index levels that should never appear as 'current'.
    # Use [^\n] so we cross decimals like '+2.1% to 5,980'.
    (
        "historic_spx_level",
        re.compile(r"\bS&P\s*500[^\n]{0,60}?\b(5[,.]?[5-9]\d{2})\b", re.IGNORECASE),
        Severity.HIGH,
        "S&P level in the 5,500–5,999 band — characteristic of late 2024. Current level is in facts.",
    ),
    # Historic CPI prints from 2024.
    # The regex captures the numeric value (group 1) so scan() can
    # compare it against the live CPI from facts before flagging.  When the
    # matched value is within ±0.2pp of the real current print the finding is
    # downgraded to LOW (still logged for observability, not auto-blocked).
    (
        "historic_cpi_2024",
        re.compile(r"\b(?:cpi|inflation|consumer\s+price)[^\n]{0,60}?\b((?:2\.4|2\.5|2\.6))\s*%", re.IGNORECASE),
        Severity.MEDIUM,
        "CPI YoY in 2.4–2.6% band — may be stale 2024 data or a real current print; cross-check against facts.",
    ),
    # Generic fed-action catcher (catches everything above + more)
    (
        "fed_action_generic",
        re.compile(r"\b(fed|fomc|powell|federal\s+reserve)\b[^.]{0,50}\b(cut|hike|slash|raise|lower|trim|reduce|ease|hiked|cut\s+rates?)", re.IGNORECASE),
        Severity.LOW,
        "Generic Fed-action phrasing — logged for observability.",
    ),
]


def _is_recent_fed_action_real(facts: Dict[str, Any]) -> bool:
    """Return True iff facts say a FOMC meeting happened in the last N days
    (where N = the window we'd credibly call 'today'/'yesterday' in copy).
    """
    today = facts.get("today")
    last = facts.get("last_fomc")
    if not today or not last:
        return False
    try:
        from datetime import date
        ty = date.fromisoformat(today)
        ly = date.fromisoformat(last)
        return (ty - ly).days <= 2  # only flag-clearing if last meeting was within 2 days
    except Exception:
        return False


def _is_current_spx_in_band(facts: Dict[str, Any], match_text: str,
                            tolerance: float = 0.03) -> bool:
    """True iff the matched S&P 500 number is within `tolerance` of
    the live SPX in the facts block. Avoids auto-blocking morning audio in
    April 2026 when current SPX is genuinely ~5500-5999.

    `match_text` is the regex group that captured the index level (e.g.
    "5,800" or "5980"). Strips commas and parses the float.
    """
    cur = facts.get("spx") or facts.get("spx_close") or facts.get("SPX")
    if cur is None:
        return False
    try:
        cur = float(cur)
        cleaned = (match_text or '').replace(',', '').strip()
        # The regex captures only the trailing 4-digit-ish number — strip
        # currency / whitespace just in case.
        cleaned = cleaned.lstrip('$').replace(',', '')
        val = float(cleaned)
        if cur <= 0 or val <= 0:
            return False
        return abs(val - cur) / cur <= tolerance
    except (TypeError, ValueError):
        return False


def _is_current_cpi_in_band(facts: Dict[str, Any], match_text: str,
                             tolerance_pp: float = 0.2) -> bool:
    """True iff the matched CPI value is within `tolerance_pp`
    percentage-points of the live CPI in the facts block.

    Current CPI prints are genuinely in the 2.4–2.6% range as of early 2026,
    so a static regex that blanket-blocks these values produces false positives.
    Compare the matched number against the real reading from facts and only
    flag if it's meaningfully off.

    `match_text` is the captured numeric string (e.g. "2.4" or "2.6").
    `tolerance_pp` is in percentage points (default ±0.2pp).

    Does NOT fall back to facts['cpi_latest']/['cpi']/['CPI'] when cpi_yoy
    is absent: those keys hold the raw CPIAUCSL INDEX level (~333), not a percent — comparing a
    "2.4%" mention against 333 is a UNITS mismatch, not a real cross-check.
    Now that a HIGH finding can block publish, use ONLY cpi_yoy: when it's
    missing we genuinely cannot verify the mention, and "unverifiable" must
    never be conflated with "confirmed wrong" (see the missing-fact branch
    below, which leaves severity at the caller's default rather than
    upgrading it).
    """
    cur = facts.get("cpi_yoy")
    if cur is None:
        # No live data — can't verify either way; leave severity unchanged.
        return False
    try:
        cur = float(cur)
        val = float((match_text or '').strip())
        return abs(val - cur) <= tolerance_pp
    except (TypeError, ValueError):
        return False


def scan(text: str, facts: Optional[Dict[str, Any]] = None) -> List[Finding]:
    """Scan generated text for training-era leak patterns.

    Returns findings ordered by span position. Caller inspects severity:
    HIGH → block publish, MEDIUM/LOW → log.
    """
    if not text or os.environ.get("GROUNDING_ENABLED", "1") == "0":
        return []

    facts = facts or {}
    findings: List[Finding] = []
    # Track which (pattern_name, span) we've emitted to avoid duplicates
    seen: set = set()

    for name, pattern, default_sev, reason in _PATTERNS:
        for m in pattern.finditer(text):
            key = (name, m.start())
            if key in seen:
                continue
            seen.add(key)

            # Severity downgrade: if facts prove the claim is legitimately current
            sev = default_sev
            if name in ("fabricated_fed_event", "rate_cut_quantum") and _is_recent_fed_action_real(facts):
                # A real FOMC just happened — the claim might actually be true.
                # Downgrade to MEDIUM so a human eyes it, don't auto-block.
                sev = Severity.MEDIUM
            elif name == "historic_spx_level":
                # The regex flags any 5,500-5,999 reading, but the
                # current SPX is in that band right now (April 2026). Compare
                # the matched number to live SPX — if within ±3% it's real,
                # not training-data leak. Downgrade to LOW so we still log
                # but don't auto-block the audio briefing.
                captured = m.group(1) if m.lastindex else m.group(0)
                if _is_current_spx_in_band(facts, captured, tolerance=0.03):
                    sev = Severity.LOW
            elif name == "historic_cpi_2024":
                # The 2.4–2.6% range that triggered on 2024 training
                # data is also the real current CPI as of early 2026.  Compare
                # captured value to live CPI from facts — if within ±0.2pp it's
                # a legitimate current print, not a hallucination.  Downgrade to
                # LOW so we still log but don't block the briefing.
                captured = m.group(1) if m.lastindex else m.group(0)
                if _is_current_cpi_in_band(facts, captured, tolerance_pp=0.2):
                    sev = Severity.LOW

            ctx_start = max(0, m.start() - 80)
            ctx_end = min(len(text), m.end() + 120)
            findings.append(Finding(
                pattern_name=name,
                severity=sev,
                match_text=m.group(0),
                span=(m.start(), m.end()),
                context=text[ctx_start:ctx_end],
                reason=reason,
            ))

    findings.sort(key=lambda f: f.span[0])
    return findings


def has_high(findings: List[Finding]) -> bool:
    return any(f.severity == Severity.HIGH for f in findings)


def filter_severity(findings: List[Finding], min_severity: Severity) -> List[Finding]:
    order = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}
    return [f for f in findings if order[f.severity] >= order[min_severity]]


# --- Calendar fabrication tripwire -----------------------------------
# Known macro-release keywords the model tends to hallucinate into a calendar.
_RELEASE_KEYWORDS = re.compile(
    r"\b(PMI|CPI|PCE|GDP|non[- ]?farm|payrolls?|NFP|jobless\s+claims|"
    r"initial\s+claims|retail\s+sales|ISM|durable\s+goods|housing\s+starts|"
    r"personal\s+income|consumer\s+confidence|FOMC)\b", re.IGNORECASE)

_DATE_IN_ROW = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.IGNORECASE)

# A consensus/expected figure: a signed/decimal percent or a 2+digit decimal.
_CONSENSUS_NUM = re.compile(r"[-+]?\d+\.\d+%?|\b\d{2,}\.\d\b")


def _source_event_tokens(source_events: List[Dict[str, Any]]) -> set:
    toks = set()
    for e in source_events or []:
        for k in ("title", "event_type"):
            v = (e.get(k) or "")
            for w in re.findall(r"[a-z]{3,}", v.lower()):
                toks.add(w)
    return toks


import re as _re

_CONF_LEVEL_CTX = _re.compile(r"support|resistance|confluence|zone", _re.IGNORECASE)
_CONF_NUM = _re.compile(r"\d[\d,]*(?:\.\d+)?")

# A number is NOT a price level when what follows it is a unit or an ordinal:
# "94th percentile", "1.9%", "37 % of range", "25 bps". The ordinal case is the
# one to get right — checking only for a literal '%' in the very next
# character would read every "Nth percentile" in the prose as a fabricated level.
_CONF_NOT_A_LEVEL = _re.compile(
    r"\s*(?:%|percent(?:ile|age)?\b|pct\b|bps\b|basis\s+points?\b|(?:st|nd|rd|th)\b)",
    _re.IGNORECASE)


def scan_confluence_fabrication(report_text, focus_list, tol=0.005):
    """Flag a confluence level tied to a focus asset that isn't within `tol` of
    any grounded level for that asset. Only numbers in a plausible price range
    for the asset (0.5x-2x its price) are checked, so MA periods (50/200), week
    counts, and years are never false-flagged.

    TABLE-AWARE: for markdown table data rows (lines whose stripped form starts
    with ``|``), the first cell is treated as the asset name and every number in
    the row is checked WITHOUT requiring a support/resistance keyword — a LiF
    table row IS level data by definition.  Header / separator rows are skipped
    automatically (their first cell won't match any grounded asset name).

    For NON-table lines: a support/resistance/confluence/zone keyword must appear
    on the line before numbers are checked, and each number is attributed to the
    NEAREST ASSET NAME PRECEDING IT rather than to every asset named on the line.
    The LiF prose is one long paragraph covering all three in-focus assets, so
    checking each number against every asset named on the line would test it
    against every asset's grounded set and flag the two it did not belong to —
    misattribution that would be the bulk of this scanner's false positives.

    Grounding uses ``focus['levels']`` (every level the confluence engine
    computed: CPR pivots, bands, EMAs, swings, key levels) when present, falling
    back to the nearest support/resistance/price for callers passing the older
    focus shape.
    """
    findings = []
    grounded, prices = {}, {}
    for f in focus_list or []:
        levels = [float(v) for v in (f.get("levels") or [])]
        for k in ("support", "resistance"):
            z = f.get(k)
            if z and z.get("price"):
                levels.append(float(z["price"]))
        if f.get("price"):
            levels.append(float(f["price"]))
            prices[f["asset"].upper()] = float(f["price"])
        grounded[f["asset"].upper()] = levels

    def _is_level(line, mt):
        """False when the number is a percentage, ordinal or unit-suffixed
        figure ('94th percentile', '1.9%', '25 bps') rather than a price."""
        return not _CONF_NOT_A_LEVEL.match(line, mt.end())

    def _flag(m, asset, levels, px):
        try:
            val = float(m.replace(",", ""))
        except ValueError:
            return
        if px and not (px * 0.5 <= val <= px * 2):
            return
        if not px and val < 1:
            return
        if not any(abs(val - lv) <= tol * lv for lv in levels):
            findings.append({"type": "fabricated_level",
                             "detail": f"{asset}: level '{m}' not in grounded confluence levels"})

    def _check_numbers(line, asset, levels, px):
        """Check every number in *line* in the plausible price range for *asset*
        against its grounded *levels*. Appends to *findings* in place."""
        for mt in _CONF_NUM.finditer(line):
            if _is_level(line, mt):
                _flag(mt.group(0), asset, levels, px)

    def _asset_spans(line):
        """Ordered (position, asset) for every whole-word asset mention."""
        spans = []
        for asset in grounded:
            for mt in _re.finditer(r"\b" + _re.escape(asset) + r"\b", line, _re.IGNORECASE):
                spans.append((mt.start(), asset))
        spans.sort()
        return spans

    for line in (report_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            # Markdown table row — parse the first cell as the asset name.
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not cells:
                continue
            first_cell = cells[0].upper()
            # Skip separator rows (e.g. |---|---|) and header rows whose first
            # cell is not a grounded asset name.
            if first_cell not in grounded:
                continue
            asset = first_cell
            # Check the entire row (all cells) for ungrounded numbers.
            _check_numbers(stripped, asset, grounded[asset], prices.get(asset, 0))
        else:
            # Prose line — require a level-context keyword before checking.
            if not _CONF_LEVEL_CTX.search(line):
                continue
            spans = _asset_spans(line)
            if not spans:
                continue
            for mt in _CONF_NUM.finditer(line):
                if not _is_level(line, mt):
                    continue
                # Attribute to the last asset named before this number, falling
                # back to the first asset on the line for a leading number
                # ("Resistance at 58,500 caps BTC") so attribution costs no recall.
                owner = spans[0][1]
                for pos, asset in spans:
                    if pos < mt.start():
                        owner = asset
                    else:
                        break
                _flag(mt.group(0), owner, grounded[owner], prices.get(owner, 0))
    return findings


#
# The bare "conflict" alternative below carries a negative lookbehind for
# "signal " — Carrac's own report vocabulary uses "Signal conflict(s)" as a
# fixed product phrase (morning_engine/prompts.py, "Signal summary" vs
# "Signal conflicts" address indicator disagreement, not a geopolitical
# event) and a line like "Signal conflict active: ... the weekly structure
# holds" was tripping this scanner as an unhedged_event_claim purely off
# that phrase pairing with the "holds" in-effect verb below. A genuine bare
# "conflict" (no "signal " immediately before it) still trips.
_NEWS_EVENT = re.compile(r"\b(treaty|waivers?|sanctions?|ceasefire|accord|moratorium|embargo|tariffs?|memorandum|mou|coup|annexation|de-?escalation|escalation|strikes?|airstrikes?|attacks?|invasion|war|(?<!signal )conflicts?|blockade|truce|withdrawal|normalization|normalisation)\b", re.IGNORECASE)
_NEWS_DECL = re.compile(
    r"\b(issued|signed|announced|imposed|lifted|struck|declared|enacted|approved|brokered|"
    # "in-effect" verbs — e.g. ("the
    # ceasefire MOU reopens the Strait of Hormuz") used none of the
    # declaration verbs above. Safe to broaden: _NEWS_EVENT must ALSO match
    # on the same line, so price-action "holds/reopens" never trips alone.
    r"reach(?:ed|es)?|ratifi(?:ed|es)|finali[sz]ed|reopen(?:s|ed|ing)?|hold(?:s|ing)?|"
    r"(?:take|takes|took)\s+effect|in\s+force|in\s+effect|come(?:s)?\s+into\s+force)\b",
    re.IGNORECASE)
_NEWS_HEDGE = re.compile(r"\b(reportedly|reported|unconfirmed|according to|alleged|rumou?red|claims?|sources say|per reports?)\b", re.IGNORECASE)


def scan_news_fabrication(report_text):
    """Flag a line that states a breaking macro EVENT (treaty/sanction/waiver/
    ceasefire/tariff/...) as settled fact with no attribution/hedge. Scheduled
    data releases and price action are ignored (no event noun)."""
    findings = []
    for line in (report_text or "").splitlines():
        if _NEWS_EVENT.search(line) and _NEWS_DECL.search(line) and not _NEWS_HEDGE.search(line):
            findings.append({"type": "unhedged_event_claim", "detail": line.strip()[:160]})
    return findings


_LEADERSHIP_EVENT = re.compile(
    r"\b(new|newly|incoming|next|nominated|appointed|confirmed)\s+"
    r"(?:appointed\s+)?"
    r"(?:fed\s+|ecb\s+|boe\s+|boj\s+)?"
    r"(chair(?:man|woman)?|president|governor|secretary)\b",
    re.IGNORECASE)


def scan_leadership_fabrication(report_text):
    """Flag a central-bank/institutional leadership CHANGE stated with no
    hedge — the 'new Chair Kevin Warsh' fabrication class.
    Mirrors scan_news_fabrication's shape: MEDIUM, alert-not-block."""
    findings = []
    for line in (report_text or "").splitlines():
        if _LEADERSHIP_EVENT.search(line) and not _NEWS_HEDGE.search(line):
            findings.append({"type": "unhedged_leadership_claim", "detail": line.strip()[:160]})
    return findings


# Non-calendar economic releases (PCE/GDP/NFP/CPI are grounded in the calendar — excluded).
_DATA_RELEASE = re.compile(
    r"\b(manufacturing pmi|services pmi|composite pmi|\bpmi\b|home sales|new home sales|"
    r"existing home sales|jobless claims|initial claims|continuing claims|retail sales|"
    r"durable goods|factory orders|ism|consumer confidence|consumer sentiment|"
    r"industrial production|housing starts|building permits|trade balance|"
    r"producer prices|import prices)\b", re.IGNORECASE)
# A print-shaped figure: decimal, percent, or k/M/B-suffixed (NOT a bare small integer like "3 months").
_RELEASE_FIGURE = re.compile(r"\b\d+\.\d+%?\b|\b\d+(?:\.\d+)?\s*[kKmMbB]\b|\b\d{2,}%")


def scan_data_release_fabrication(report_text):
    """Flag a non-calendar economic-release figure stated as bare fact (no hedge).
    Reuses the news hedge list. Print-shaped numbers only — bare small integers
    (e.g. '3 months') are ignored to avoid false positives."""
    findings = []
    for line in (report_text or "").splitlines():
        if (_DATA_RELEASE.search(line) and _RELEASE_FIGURE.search(line)
                and not _NEWS_HEDGE.search(line)):
            findings.append({"type": "unattributed_release_figure", "detail": line.strip()[:160]})
    return findings


def scan_calendar_fabrication(report_text: str, source_events: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Flag economic-calendar events/consensus in the report not grounded in
    ``source_events`` (the ONLY allowed events, from quant.events).

    Consensus is currently always absent in the source, so any numeric Expected
    on a calendar row is fabrication. Returns a list of findings (empty = clean).
    """
    findings: List[Dict[str, str]] = []
    allowed = _source_event_tokens(source_events)
    for line in (report_text or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        joined = " ".join(cells)
        if not _DATE_IN_ROW.search(joined):
            continue  # header / separator / non-calendar row
        kw = _RELEASE_KEYWORDS.search(joined)
        if not kw:
            continue  # a dated row with no recognised release keyword — ignore
        token = re.sub(r"[\s\-]", "", kw.group(0).lower())
        grounded = any(token.startswith(a[:4]) or a.startswith(token[:4]) for a in allowed)
        if not grounded:
            findings.append({"type": "fabricated_event",
                             "detail": f"calendar row cites '{kw.group(0)}' not in grounded events"})
        for cell in cells:
            if cell in ("—", "-", ""):
                continue
            if _CONSENSUS_NUM.search(cell) and not _DATE_IN_ROW.search(cell):
                findings.append({"type": "fabricated_consensus",
                                 "detail": f"calendar row has consensus '{cell}' but source provides none"})
                break
    return findings


# ---------------------------------------------------------------------
# Market Snapshot table internal-consistency scans.
#
# Both scanners below check a generated markdown TABLE cell against a
# deterministic ground-truth dict the caller supplies, rather than scanning
# for training-data-bleed patterns like the scanners above. They exist
# because the free public /brief's LLM-authored Market Snapshot tables can
# restate numbers that disagree with the deterministic
# figures rendered elsewhere on the SAME page:
#   (a) Table 1's "Range" column vs the Cross-Asset Heat strip's
#       range_pctile.pct (SPX 83% vs 98%, GOLD 89% vs 52%, BTC 42% vs 51%).
#   (b) Table 3's Rotation Scorecard "Status" vs the actual RRG quadrant
#       (a BTC/ETH/TOTAL row called "Lagging" when BTC/ETH were Leading —
#       TOTAL's quadrant had been generalized onto the whole row).
# Both are non-blocking (see morning_engine.engine._range_pctile_tripwire /
# _rotation_quadrant_tripwire) — defense-in-depth alongside the source-data
# fix in morning_engine.data_assembly.format_compact_asset_lines(), which
# stops the model being fed the wrong number in the first place.
# ---------------------------------------------------------------------

_TABLE_SEPARATOR_ROW = re.compile(r"^[\s\-:|]+$")


def scan_range_pctile_mismatch(
    report_text: str, grounded_pct: Dict[str, int], tol: int = 5
) -> List[Dict[str, str]]:
    """Flag a Market Snapshot 'Range' table cell that disagrees with the
    deterministic range_pctile the model was given as ground truth.

    ``grounded_pct``: {SYMBOL: pct int 0-100} — the exact per-asset numbers
    handed to the model verbatim (morning_engine.data_assembly.
    format_compact_asset_lines' range_pctile_map). Only assets present in
    this dict are checked; a row for an asset absent from it (e.g. a
    spotlight asset, which doesn't appear in this table) is never flagged
    on missing ground truth.

    Table-aware: locates the header row containing both an 'Asset' and a
    'Range' column (any casing / minor variant, e.g. 'Range %'), then reads
    every following data row's Range cell until a non-table line ends it.
    """
    findings: List[Dict[str, str]] = []
    if not grounded_pct:
        return findings

    range_col: Optional[int] = None
    asset_col: Optional[int] = None
    for line in (report_text or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            range_col = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if range_col is None:
            lowered = [c.lower() for c in cells]
            asset_idx = next((i for i, c in enumerate(lowered) if c == "asset"), None)
            range_idx = next((i for i, c in enumerate(lowered) if "range" in c), None)
            if asset_idx is not None and range_idx is not None:
                asset_col, range_col = asset_idx, range_idx
            continue
        if _TABLE_SEPARATOR_ROW.match(s.strip("|")):
            continue
        if range_col >= len(cells) or asset_col >= len(cells):
            continue
        asset = cells[asset_col].upper().strip("*_ ")
        if asset not in grounded_pct:
            continue
        m = re.search(r"(\d{1,3})\s*%", cells[range_col])
        if not m:
            continue
        stated = int(m.group(1))
        truth = grounded_pct[asset]
        if abs(stated - truth) > tol:
            findings.append({
                "type": "range_pctile_mismatch",
                "detail": f"{asset}: table states Range {stated}% but the grounded "
                          f"90-day range_pctile is {truth}%",
            })
    return findings


_QUADRANT_WORDS = ("leading", "improving", "weakening", "lagging")


def scan_rotation_quadrant_mismatch(
    report_text: str, grounded_quadrant: Dict[str, str]
) -> List[Dict[str, str]]:
    """Flag a Rotation Scorecard row whose Status disagrees with the actual
    RRG quadrant of an asset NAMED in that row's Sector cell.

    ``grounded_quadrant``: {SYMBOL: 'leading'|'improving'|'weakening'|
    'lagging'} (lowercase), from the SAME RS/RRG cache the page's own RS
    Rankings table and Rotation strip read (carrac.charts.routes.
    _compute_rs_rankings / carrac.facts.bands.build_rotation).

    Catches the generalization failure mode where a row groups several
    assets (e.g. 'BTC/ETH/TOTAL') under one Status word that only actually
    describes one member — every OTHER named asset whose grounded quadrant
    differs from the stated Status is flagged individually.
    """
    findings: List[Dict[str, str]] = []
    if not grounded_quadrant:
        return findings

    symbol_re = re.compile(
        r"\b(" + "|".join(re.escape(s) for s in sorted(grounded_quadrant, key=len, reverse=True)) + r")\b"
    )
    status_col: Optional[int] = None
    sector_col: Optional[int] = None
    for line in (report_text or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            status_col = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if status_col is None:
            lowered = [c.lower() for c in cells]
            status_idx = next((i for i, c in enumerate(lowered) if c == "status"), None)
            sector_idx = next((i for i, c in enumerate(lowered) if "sector" in c or "asset" in c), None)
            if status_idx is not None and sector_idx is not None:
                status_col, sector_col = status_idx, sector_idx
            continue
        if _TABLE_SEPARATOR_ROW.match(s.strip("|")):
            continue
        if status_col >= len(cells) or sector_col >= len(cells):
            continue
        status_lower = cells[status_col].lower()
        stated_quadrant = next((w for w in _QUADRANT_WORDS if w in status_lower), None)
        if not stated_quadrant:
            continue
        for mt in symbol_re.finditer(cells[sector_col].upper()):
            symbol = mt.group(1)
            truth = grounded_quadrant.get(symbol)
            if truth and truth != stated_quadrant:
                findings.append({
                    "type": "rotation_quadrant_mismatch",
                    "detail": f"{symbol}: row states '{stated_quadrant}' but the grounded "
                              f"RRG quadrant is '{truth}'",
                })
    return findings


# ---------------------------------------------------------------------------
# Internal arithmetic
# ---------------------------------------------------------------------------

# "from $84 to $71.90 (-22.8%)", "84 -> 71.90, -22.8%", "84 → 71.90 (-22.8%)".
# Requires BOTH endpoints and the percentage within one clause, because that
# is what makes the claim self-checkable: two printed numbers imply the third.
_TRANSITION_RE = re.compile(
    r"""
    \$?\s*(?P<a>\d[\d,]*(?:\.\d+)?)      # start value
    \s*(?:->|-->|→|to)\s*                # transition marker
    \$?\s*(?P<b>\d[\d,]*(?:\.\d+)?)      # end value
    (?P<gap>[^.;:\n%()]{0,25}?)          # SHORT connective, same clause only
    \(?\s*(?P<pct>[-+−]\s?\d+(?:\.\d+)?)\s*%
    """,
    re.VERBOSE,
)

# A conjunction between the prices and the percentage means the percentage
# belongs to a DIFFERENT subject and the arithmetic was never claimed.
# Measured against 60 stored prod reports: without this the scanner's only
# hit was "28 to 22 (Extreme Fear) while open interest rose +4.2%" — the
# Fear-and-Greed index moving 28->22 and a separate open-interest figure,
# bridged into a false accusation. That was a 100% false-positive rate on
# the corpus, i.e. exactly the kind of alarm that turns into noise.
_CLAUSE_BREAK_RE = re.compile(
    # Sentence-ending punctuation breaks a clause too — a replay
    # caught "Fear & Greed reads 27. DVOL sits at cycle lows" attributing the
    # previous sentence's 27 to DVOL. A period, colon before a list, or a
    # newline is at least as strong a boundary as "while".
    # A dot NOT followed by a digit ends a sentence ("reads 27. DVOL...");
    # a dot followed by a digit is a decimal point ("0.45") and must not
    # split, or every decimal would become a phantom "states 0". ":" is a
    # label-value connector, never a break.
    r"[!?;\n]|\.(?!\d)|"
    r"\b(?:while|and|but|as|though|whereas|with|versus|vs|against|amid|after|before)\b",
    re.IGNORECASE,
)


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(str(raw).replace(",", "").replace("−", "-").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def scan_internal_arithmetic(report_text: str, tol_pct: float = 1.0) -> List[Dict[str, str]]:
    """Flag a stated percentage move that its own two printed prices refute.

The weekly report contradicted itself in 6 of 9 audited
    editions. The cleanest instance: "Oil 84 -> 71.90 ... -22.8%", where the
    two prices actually imply -14.4%. Nothing upstream can catch that — the
    prices are real and the percentage is real, they simply are not the same
    claim. It is only detectable AFTER generation, from the printed text.

    Deliberately narrow. It fires only when a start value, an end value and
    a percentage appear in ONE clause, so the arithmetic is fully determined
    by what the reader can see. A percentage quoted on its own is not
    checkable here and is left alone — that class belongs to the
    compute-at-source fix (hand the model one precomputed delta per asset and
    window, as was done for the Range column), not to a text scan.

    `tol_pct` is in percentage POINTS and defaults to 1.0. Rounding in the
    prose ("roughly 14%") must not fire; -22.8 against -14.4 is 8.4 points
    out and always will.
    """
    findings: List[Dict[str, str]] = []
    for m in _TRANSITION_RE.finditer(report_text or ""):
        a, b, stated = _to_float(m.group("a")), _to_float(m.group("b")), _to_float(m.group("pct"))
        if a is None or b is None or stated is None or a == 0:
            continue
        # A conjunction between the prices and the percentage means the
        # percentage is about something else entirely — see _CLAUSE_BREAK_RE.
        if _CLAUSE_BREAK_RE.search(m.group("gap") or ""):
            continue
        # Guard against matching unrelated numbers (a year, a count) that
        # happen to sit near a percentage: a real price transition of >90%
        # in one clause is far more likely to be a bad match than a real move.
        implied = (b - a) / a * 100.0
        if abs(implied) > 90:
            continue
        if abs(implied - stated) <= tol_pct:
            continue
        findings.append({
            "type": "internal_arithmetic_mismatch",
            "detail": (
                f"'{m.group(0).strip()}' — {a:g} to {b:g} is {implied:+.1f}%, "
                f"not {stated:+.1f}%"
            ),
        })
    return findings


# ---------------------------------------------------------------------------
# A stated field must agree with its own deterministic ground
# truth (a BTC GEX flip point published as 165,694 when quant.derivatives
# held 69,109.33 for that day — 2.6x off, and no existing scanner had a rule
# for the field: _confluence_tripwire is scoped to the Levels-in-Focus
# section only, and this claim sat in ordinary narrative prose).
# ---------------------------------------------------------------------------

# A number in the plausible formats this scans for: bare, comma-grouped, or
# $-prefixed. $165k-style abbreviations are explicitly out of scope.
#: Magnitude suffixes, compact and spelled out. Open interest is
#: rendered — and narrated — as "$8.46B", which a regex starting at the digit would read as the
#: number 8.46 and then compare against 8,460,000,000. That is a guaranteed
#: false alarm on a CORRECT report, which is the one failure mode this scanner
#: must not have.
_FIELD_MAGNITUDES = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "million": 1e6,
    "b": 1e9, "billion": 1e9,
    "t": 1e12, "trillion": 1e12,
}

_FIELD_MAG_SUFFIX = r"(?:[KMBT]\b|thousand|million|billion|trillion)"

# A leading sign, either before or after the "$" ("-$4.2M" from
# `_fmt_usd_compact`, "-4.2%" from `_fmt_pct_signed`), must be part of the
# match — a pattern starting at the digit would silently drop it,
# which reads every correctly-stated NEGATIVE funding rate as its positive
# mirror and manufactured a mismatch against the (also negative) truth.
_FIELD_SIGN = r"[-+−]?"

_FIELD_NUM_RE = re.compile(
    _FIELD_SIGN + r"\$?\s*" + _FIELD_SIGN
    + r"\d[\d,]*(?:\.\d+)?(?:\s*" + _FIELD_MAG_SUFFIX + r")?",
    re.IGNORECASE,
)

_FIELD_MAG_RE = re.compile(_FIELD_MAG_SUFFIX + r"$", re.IGNORECASE)

_FIELD_POINT_UNITS = ("points", "pts", "point", "pp")

# A number immediately followed by an ordinal suffix ("90th", "8th", "1st")
# is a PERCENTILE, never the field's own value — every field this scanner
# checks (OI, ratio, annualised rate) is reported in the same prose next to
# its own percentile, and the ordinal number sits at least as close to the
# label as the real one does.
_ORDINAL_SUFFIX_RE = re.compile(r"^(?:st|nd|rd|th)\b", re.IGNORECASE)

# A number
# immediately followed by a DURATION unit ("90-day", "7 days", "3h") is
# describing a lookback window, never the field's own value — same shape as
# the ordinal/percent guards above, just a different neighbouring word.
_DURATION_SUFFIX_RE = re.compile(
    r"^[-\s]*(?:days?|d|hours?|h|weeks?|wk|months?|minutes?|min|years?|yr)\b",
    re.IGNORECASE,
)


def _field_num_to_float(raw: str) -> Optional[float]:
    text = (raw or "").strip()
    sign = 1.0
    # The sign can sit before the "$" or between it and the digits; strip it
    # from either position rather than assuming one.
    for _ in range(2):
        if text[:1] in ("-", "+", "−"):
            if text[0] != "+":
                sign = -sign
            text = text[1:].strip()
        elif text[:1] == "$":
            text = text[1:].strip()
    multiplier = 1.0
    mag = _FIELD_MAG_RE.search(text)
    if mag:
        multiplier = _FIELD_MAGNITUDES[mag.group(0).lower()]
        text = text[:mag.start()].strip()
    value = _to_float(text)
    return None if value is None else sign * value * multiplier


# `_fmt_usd_compact` itself switches to a magnitude-suffixed form only at
# $1M and above (below that it prints a plain "$x,xxx"/"x,xxx"). A "usd"
# claim whose truth sits below that line (a GEX flip point, a max-pain
# strike) is legitimately stated bare, exactly like the original
# GEX fixture — so the $-or-suffix requirement below must only bite for the
# compact-scale case (open interest), never for a price-like usd field.
_FIELD_COMPACT_USD_FLOOR = 1e6


def _field_candidate_ok(
    seg: str, m: "re.Match[str]", unit: Optional[str], truth: Optional[float] = None
) -> bool:
    """Reject a matched number that cannot plausibly be THIS field's value.

    Three families of `grounded field mismatch` noise all had
    the same shape: the nearest number to the label was some OTHER figure
    that happens to sit close by (a percentile, a %-change), not the value
    the writer meant. Filtering those candidates out is what makes the
    remaining nearest-number search trustworthy again, rather than loosening
    tolerances until real mismatches stop firing too.
    """
    tail = seg[m.end():m.end() + 3]
    if _ORDINAL_SUFFIX_RE.match(tail):
        return False
    # A lookback window ("90-day history", "over 7
    # days") reads as a number right next to the label just like a
    # percentile ordinal does — reject it the same way, by what immediately
    # follows the number, rather than inventing a second mechanism.
    duration_tail = seg[m.end():m.end() + 12]
    if _DURATION_SUFFIX_RE.match(duration_tail):
        return False
    if unit == "usd" and truth is not None and abs(truth) >= _FIELD_COMPACT_USD_FLOOR:
        # Open interest is always rendered via `_fmt_usd_compact` at this
        # scale — always $-prefixed or magnitude-suffixed. A bare, unmarked
        # number near "open interest" is some other statistic (a %-change is
        # the observed case), not the OI figure, and must not be compared.
        if "$" not in m.group(0) and not _FIELD_MAG_RE.search(m.group(0)):
            return False
    if unit == "ratio" and seg[m.end():m.end() + 1] == "%":
        # A long/short ratio is never itself a percentage.
        return False
    return True


def _field_clause_forward(line: str, start: int, window: int = 80) -> str:
    """Text after `start`, truncated at the window and at the first clause
    break — reuses the lesson that a conjunction/preposition
    between a label and a number usually means the number belongs to a
    different claim."""
    seg = line[start:start + window]
    m = _CLAUSE_BREAK_RE.search(seg)
    return seg[:m.start()] if m else seg


def _field_clause_backward(line: str, end: int, window: int = 80) -> str:
    """Text before `end`, truncated at the window and clipped to only what
    follows the LAST clause break inside that window (the same-clause rule,
    applied looking backward from the label)."""
    seg = line[max(0, end - window):end]
    last_break = None
    for m in _CLAUSE_BREAK_RE.finditer(seg):
        last_break = m
    return seg[last_break.end():] if last_break else seg


def _nearest_field_number(
    line: str, label_match, unit: Optional[str] = None, truth: Optional[float] = None
) -> Optional[float]:
    """The nearest number to `label_match`, within ~80 chars and the SAME
    clause, searching both forward and backward from the label and taking
    whichever is closer. None when no in-clause number is found — that is a
    silent result, not a guess.

    Candidates that cannot plausibly be this field's own value
    (a percentile ordinal, or — for a compact-scale "usd" field — a bare
    number with no $/magnitude marker) are skipped rather than picked just
    for being nearest; see `_field_candidate_ok`.
    """
    candidates: List[tuple] = []

    fwd_seg = _field_clause_forward(line, label_match.end())
    for fwd_m in _FIELD_NUM_RE.finditer(fwd_seg):
        if not _field_candidate_ok(fwd_seg, fwd_m, unit, truth):
            continue
        val = _field_num_to_float(fwd_m.group(0))
        if val is not None:
            candidates.append((fwd_m.start(), val))
        break

    back_seg = _field_clause_backward(line, label_match.start())
    back_matches = [
        bm for bm in _FIELD_NUM_RE.finditer(back_seg)
        if _field_candidate_ok(back_seg, bm, unit, truth)
    ]
    if back_matches:
        bm = back_matches[-1]
        val = _field_num_to_float(bm.group(0))
        if val is not None:
            candidates.append((len(back_seg) - bm.end(), val))

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def scan_grounded_field_mismatch(
    report_text: str, claims: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, str]]:
    """Flag a stated numeric field that disagrees with the deterministic
    ground truth the model was actually handed for it.

    ``claims``: list of dicts, one per checkable field —
        {"asset": "BTC", "field_label_regex": r"GEX\\s+flip\\s+point",
         "truth": 69109.33, "tol": 0.02, "unit": "usd"}

    ``tol`` is a FRACTION of truth (flag when |stated-truth|/truth > tol)
    unless ``unit`` is a points unit ("points"/"pts"/"pp"), in which case it
    is an absolute tolerance (flag when |stated-truth| > tol) — DVOL and
    regime-confidence claims use points; price-like claims (GEX flip point,
    max pain) use a fraction.

    For each label match, the claimed asset is resolved from the NEAREST
    PRECEDING asset mention on the same line (the ``_asset_spans`` idiom
    scan_confluence_fabrication uses) — a label whose line does not name the
    claimed asset most recently is left alone rather than guessed at. The
    stated value is the nearest number to the label within ~80 chars in the
    SAME clause (see ``_nearest_field_number`` / ``_CLAUSE_BREAK_RE``); a
    label with no in-clause number, or one scoped to a different asset, is
    silent — this scanner is deliberately narrow for the same reason
    ``scan_internal_arithmetic`` is: a false positive here is a scanner bug,
    not a judgement call, and an unearned alarm is costly.
    """
    findings: List[Dict[str, str]] = []
    if not report_text or not claims:
        return findings

    known_assets = sorted({
        str(c.get("asset", "")).upper() for c in claims if c.get("asset")
    }, key=len, reverse=True)
    if not known_assets:
        return findings

    def _asset_spans(line: str) -> List[tuple]:
        spans = []
        for asset in known_assets:
            for mt in re.finditer(r"\b" + re.escape(asset) + r"\b", line, re.IGNORECASE):
                spans.append((mt.start(), asset))
        spans.sort()
        return spans

    lines = report_text.splitlines()

    for claim in claims:
        asset = str(claim.get("asset", "")).upper()
        truth = claim.get("truth")
        label_pattern = claim.get("field_label_regex")
        if not asset or truth is None or not label_pattern:
            continue
        try:
            truth = float(truth)
        except (TypeError, ValueError):
            continue
        if truth == 0:
            continue
        tol = claim.get("tol", 0.02)
        unit = str(claim.get("unit") or "").lower()
        is_points = unit in _FIELD_POINT_UNITS
        if unit == "ratio":
            # The table renders a ratio to 2 decimals but prose
            # commonly restates it to 1 ("2.5868" -> "about 2.6", a 4.4%
            # relative difference) — a real rounding, not a fabrication. 5%
            # covers single-decimal rounding of every ratio this block
            # produces (they run ~0.3-5) without hiding a genuinely
            # different number (see the "still flags" test at 2x+ off).
            tol = max(tol, 0.05)
        try:
            label_re = re.compile(label_pattern, re.IGNORECASE)
        except re.error:
            continue

        for line in lines:
            spans = _asset_spans(line)
            if not spans:
                continue
            for lm in label_re.finditer(line):
                owner = spans[0][1]
                for pos, a in spans:
                    if pos < lm.start():
                        owner = a
                    else:
                        break
                if owner != asset:
                    continue
                stated = _nearest_field_number(line, lm, unit=unit, truth=truth)
                if stated is None:
                    continue

                if is_points:
                    diff = abs(stated - truth)
                    bad = diff > tol
                    off_str = f"{diff:.1f} pts"
                else:
                    diff = abs(stated - truth) / truth
                    bad = diff > tol
                    off_str = f"{diff * 100:.1f}%"
                if bad:
                    findings.append({
                        "type": "grounded_field_mismatch",
                        "detail": (
                            f"{asset}: '{lm.group(0).strip()}' states {stated:g}, "
                            f"grounded value is {truth:g} ({off_str} off)"
                        ),
                    })
    return findings
