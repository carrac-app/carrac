"""
Briefing assembly helpers — text extraction, priority parsing, shared
briefing builder for morning + dispatch + afternoon + weekly flows.

No LLM calls here; pure text manipulation.
"""

from __future__ import annotations

import re

from .config_loader import load_derivatives_regimes


def strip_section_header(text, kind):
    """Strip leading PART/KEY POINTS/SPOKEN SCRIPT scaffolding from a parsed audio script half.

    Different models format their dual-output sections differently
    (Anthropic: "KEY POINTS", Grok: "KEY POINTS (for text display) ===" or
    "PART 2: SPOKEN SCRIPT === ..."), so a single-token strip isn't enough.
    Drop any leading lines that look like section scaffolding (=== or
    "PART x" or "KEY POINTS" or "SPOKEN SCRIPT") until we hit real content.
    """
    if not text:
        return text
    lines = text.split("\n")
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue
        upper = first.upper().lstrip("=").rstrip("=").strip()
        is_scaffold = (
            first.startswith("===")
            or first.endswith("===")
            or upper.startswith("PART 1")
            or upper.startswith("PART 2")
            or upper.startswith("KEY POINTS")
            or upper.startswith("SPOKEN SCRIPT")
            or upper == "DISPLAY"
            or upper == "SPOKEN"
        )
        if is_scaffold:
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def extract_section(report_text, section_name):
    """Extract a section's body from a markdown report by ## heading."""
    pattern = rf"^## {re.escape(section_name)}\s*\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, report_text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


# Section drop order when a report exceeds its char limit. Lowest-value
# sections drop first; Action Items / Executive Summary / Devil's Advocate
# are never auto-dropped and must be kept by the trim loop.
DEFAULT_DROP_ORDER = [
    "Market Snapshot",
    "Pattern Status Update",
    "Comparison to Yesterday",
    "Asset Spotlight",
    "Macro Context",
    "Macro",
    "Foresight Impact",
    "Foresight",
    "Play Evaluations",
    "Alerts & Positions Follow-Up",
    "Quant Refresh",
]

PROTECTED_SECTIONS = {
    "Executive Summary",
    "Action Items",
    "Devil's Advocate",
    # "Scenarios" (operator/personalized reports) and the free-tier report's
    # reframed "What Would Change the Picture" both carry the
    # confluence + base-rate data assembled by data_assembly.py, so both
    # are protected. Neither is in DEFAULT_DROP_ORDER; both guards ensure the
    # section is never silently removed by the trim loop even when a report
    # runs long.
    "Scenarios",
    "What Would Change the Picture",
    # week_ahead's opening regime synthesis and closing thesis —
    # the bookends of the briefing, same role as Executive Summary/Action
    # Items above. Never in WEEK_AHEAD_DROP_ORDER; guarded here so the trim
    # loop can't remove them even if week_ahead runs long.
    "Week Setup",
    "Thesis Going In",
}

# week_ahead's own drop order. DEFAULT_DROP_ORDER's vocabulary
# (Market Snapshot, Asset Spotlight, Macro, Foresight, Play Evaluations —
# daily-report section names) shares ZERO headings with week_ahead's actual
# sections (Week Setup, Catalyst Calendar, Setup Watch, Macro Themes, Thesis
# Going In — see prompts.py week_ahead_shared_prompt), so passing
# DEFAULT_DROP_ORDER to enforce_char_limit for week_ahead never drops
# anything and it falls straight to blunt character truncation — hence the
# "Report truncated to fit length limit." marker mid-thesis every Sunday.
#
# Ordered least-valuable-first:
#   1. Macro Themes  — broadest and most redundant section: it re-covers the
#      regime story Week Setup already synthesizes, just sliced by asset
#      class instead of as one narrative. Easiest to lose without leaving a
#      hole — nothing else in the report depends on it.
#   2. Setup Watch    — itemized scanner-trigger-proximity list. Useful but
#      perishable (state can flip within a day and is always re-checkable
#      live in the app's own scanner/Desk), and it's a mechanical list, not
#      synthesis — the least "written" section in the report.
#   3. Catalyst Calendar — the hard calendar dates ARE genuinely useful and
#      not otherwise available live, but it's the most COMPACT of the three
#      droppable sections (one table + one-line notes) and is more
#      mechanically regenerable next Sunday than a thesis is, so it still
#      ranks above the two protected bookends.
# "Week Setup" (opening regime synthesis) and "Thesis Going In" (closing
# thesis) are NOT in this list — they're in PROTECTED_SECTIONS instead,
# mirroring Executive Summary/Action Items for the daily reports: these are
# the two sections a reader least wants silently truncated out from under
# them, so if the report is still over budget after dropping all three
# below, it falls through to the existing sentence/paragraph-boundary hard
# truncate rather than losing Week Setup or Thesis Going In outright.
WEEK_AHEAD_DROP_ORDER = [
    "Macro Themes",
    "Setup Watch",
    "Catalyst Calendar",
]


def _strip_section(report_text, section_name):
    """Remove a single ## section (heading + body) from a markdown report."""
    pattern = rf"^## {re.escape(section_name)}\s*\n.*?(?=^## |\Z)"
    return re.sub(pattern, "", report_text, count=1, flags=re.MULTILINE | re.DOTALL)


def enforce_char_limit(report_text, max_chars, drop_order=None,
                        protected=None, log_callback=None):
    """Trim a generated report to fit within max_chars.

    Removes sections in priority order (lowest-value first) until under the
    limit. PROTECTED_SECTIONS are never dropped — if removing every droppable
    section still leaves the report over-limit, the protected sections are
    truncated by character count as a last resort.

    Args:
        report_text: full markdown report
        max_chars: target maximum length
        drop_order: ordered list of section names to drop first (default DEFAULT_DROP_ORDER)
        protected: set of section names to never drop (default PROTECTED_SECTIONS)
        log_callback: optional callable(msg) for trim notifications

    Returns:
        Trimmed report string, guaranteed `<= max_chars`.
    """
    if not report_text or len(report_text) <= max_chars:
        return report_text
    drop_order = drop_order or DEFAULT_DROP_ORDER
    protected = protected or PROTECTED_SECTIONS

    def _log(msg):
        if log_callback:
            log_callback(msg)

    trimmed = report_text
    original_len = len(trimmed)
    dropped = []
    for section in drop_order:
        if len(trimmed) <= max_chars:
            break
        if section in protected:
            continue
        new = _strip_section(trimmed, section)
        if len(new) < len(trimmed):
            dropped.append(section)
            trimmed = new

    if dropped:
        _log(f"enforce_char_limit: trimmed {original_len}→{len(trimmed)} "
             f"chars; dropped sections: {dropped}")

    # Hard truncate as last resort. Break on a clean boundary — paragraph,
    # then sentence, then word — NEVER mid-word ("...energy sector i…").
    if len(trimmed) > max_chars:
        cutoff = trimmed.rfind("\n\n", 0, max_chars)
        if cutoff < int(max_chars * 0.8):
            # No nearby paragraph break — try the last sentence end.
            sent = max(
                trimmed.rfind(". ", 0, max_chars),
                trimmed.rfind("? ", 0, max_chars),
                trimmed.rfind("! ", 0, max_chars),
                trimmed.rfind(".\n", 0, max_chars),
            )
            if sent >= int(max_chars * 0.6):
                cutoff = sent + 1
            else:
                # Fall back to the last whitespace before the cap (word boundary).
                ws = trimmed.rfind(" ", 0, max_chars)
                cutoff = ws if ws >= int(max_chars * 0.6) else max_chars
        trimmed = trimmed[:cutoff].rstrip() + "\n\n_[Report truncated to fit length limit.]_"
        _log(f"enforce_char_limit: hard-truncated to {len(trimmed)} chars "
             f"(protected sections still over-limit)")

    return trimmed


def trim_dangling_sentence(text):
    """Trim a trailing incomplete sentence — for when the model hit max_tokens
    and stopped mid-sentence (e.g. '...turned structurally negative. On'). Cuts
    back to the last complete sentence so a stored report never ends on a
    fragment. Gate this on stop_reason == 'max_tokens' at the call site; it is a
    safe no-op when the text already ends cleanly or has no earlier sentence break
    to fall back to (so it never nukes a legitimate header/bullet ending).
    """
    if not text:
        return text
    stripped = text.rstrip()
    # Already ends on a sentence terminator / closing markdown token — nothing dangling.
    if not stripped or stripped[-1] in '.!?:")]}>*_~`”’…':
        return text
    # Cut back to the last sentence terminator ('. ' / '! ' / '? ' / before newline).
    cut = max(
        stripped.rfind('. '), stripped.rfind('! '), stripped.rfind('? '),
        stripped.rfind('.\n'), stripped.rfind('!\n'), stripped.rfind('?\n'),
    )
    if cut < 0:
        return text  # no earlier boundary — leave as-is rather than drop content
    return stripped[:cut + 1].rstrip()


def extract_exec_summary(personal_content):
    """Extract executive summary from personal section content."""
    text = extract_section(personal_content, "Executive Summary")
    return text if text else None


def extract_tldr_summary(content):
    """Extract a plain-text summary from the shared report's ## TL;DR section.

    Fallback used when no admin personal "Executive Summary" is available (e.g.
    no admin user configured, admin personal generation failed, the personal
    section lacked the subsection, or — as in production today — only the general
    report runs).

    Mirrors _extract_afternoon_summary (pure text, no LLM) but understands the
    general/morning TL;DR, which is a BULLET list: it flattens the bullets into
    plain prose (list markers + **bold**/*italic* stripped) so the summary reads
    cleanly in every consumer — the sidebar card, the /report page, and the
    PLAIN-TEXT landing-page teaser. Returns a trimmed string (<= 300 chars) or
    None.
    """
    if not content:
        return None

    tldr_body = extract_section(content, "TL;DR")
    source = tldr_body if tldr_body else content

    parts = []
    for raw in source.replace('\r', '').split('\n'):
        line = raw.strip()
        if not line:
            continue
        # Stop at / skip structural markdown: headings, tables, horizontal rules
        if line.startswith('#') or line.startswith('|') or re.match(r'^[-*_]{3,}$', line):
            if parts:
                break
            continue
        # Flatten: drop a leading list/number marker, then inline bold/italic
        line = re.sub(r'^([-*•]|\d+[.)])\s+', '', line)
        line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
        line = re.sub(r'\*(.+?)\*', r'\1', line)
        line = line.strip()
        if len(line) < 20:
            continue
        parts.append(line)
        if len(' '.join(parts)) >= 200:
            break

    if not parts:
        return None

    result = ' '.join(parts)
    if len(result) > 300:
        cut = result.rfind('. ', 0, 300)
        if cut > 80:
            result = result[:cut + 1]
        else:
            word_cut = result.rfind(' ', 80, 300)  # last whole word before the limit
            base = result[:word_cut] if word_cut > 80 else result[:300]
            result = base.rstrip(' ,;:').rstrip() + '…'
    return result


def build_sentiment_with_priority(sentiment_text, onchain_signals):
    """Annotate sentiment text with on-chain priority markers.

    Prepends a priority summary block before the raw sentiment data so Opus
    knows which on-chain metrics are at notable extremes.
    """
    header = "## Market Sentiment\n"
    if not onchain_signals:
        return f"{header}{sentiment_text}\n"

    notable = {k: v for k, v in onchain_signals.items() if v.get("priority") == "NOTABLE"}
    if not notable:
        return f"{header}{sentiment_text}\n"

    lines = [header, "=== ON-CHAIN PRIORITY ==="]
    for metric, info in notable.items():
        val = info.get("value", "?")
        reason = info.get("reason", "")
        if metric == "fear_greed":
            lines.append(f"[NOTABLE] Fear & Greed: {val} — {reason}")
        elif metric == "mvrv":
            lines.append(f"[NOTABLE] MVRV Z-Score: {val} — {reason}")
        elif metric == "funding_rate":
            rate_pct = val * 100 if isinstance(val, (int, float)) else val
            lines.append(f"[NOTABLE] BTC Funding Rate: {rate_pct:.4f}% — {reason}")
        elif metric == "open_interest":
            lines.append(f"[NOTABLE] Open Interest: {val:+.1f}% change — {reason}")
    lines.append("=========================\n")
    lines.append(sentiment_text)
    lines.append("")
    return "\n".join(lines)


def parse_asset_priority(text):
    """Parse asset priority classification from Sonnet output.

    Expects lines like:
        CRITICAL: BTC, OIL, GOLD
        NOTABLE: ETH, SPX, QQQ
        BACKGROUND: TOTAL, MAGS, ...

    Returns: {asset: tier} dict.
    """
    priority = {}
    for line in text.split("\n"):
        line = line.strip()
        for tier in ("CRITICAL", "NOTABLE", "BACKGROUND"):
            if line.upper().startswith(tier):
                # Extract assets after the colon
                _, _, assets_str = line.partition(":")
                assets = [a.strip().upper() for a in assets_str.split(",") if a.strip()]
                for asset in assets:
                    priority[asset] = tier
                break
    return priority


def build_prioritized_chart_data(chart_data, asset_priority):
    """Restructure chart data text with priority group headers.

    Splits the formatted chart data into per-asset blocks, then regroups
    them under CRITICAL / NOTABLE / BACKGROUND headers with depth guidance.

    Returns: restructured chart data string.
    """
    if not asset_priority:
        return chart_data

    # Parse chart_data into per-asset blocks (split on "## ASSET" headers)
    asset_blocks = {}
    current_asset = None
    current_lines = []
    for line in chart_data.split("\n"):
        if line.startswith("## "):
            if current_asset:
                asset_blocks[current_asset] = "\n".join(current_lines)
            current_asset = line[3:].strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_asset:
        asset_blocks[current_asset] = "\n".join(current_lines)

    # Group assets by priority tier
    groups = {"CRITICAL": [], "NOTABLE": [], "BACKGROUND": []}
    for asset, block in asset_blocks.items():
        tier = asset_priority.get(asset, "NOTABLE")
        groups[tier].append(block)

    # Build output with priority headers
    parts = []
    header_line = chart_data.split("\n")[0] if chart_data.startswith("# ") else ""
    if header_line:
        parts.append(header_line)
        parts.append("")

    if groups["CRITICAL"]:
        parts.append("=== CRITICAL — High signal activity, drives today's thesis ===")
        parts.extend(groups["CRITICAL"])
        parts.append("")

    if groups["NOTABLE"]:
        parts.append("=== NOTABLE — Mention if relevant ===")
        parts.extend(groups["NOTABLE"])
        parts.append("")

    if groups["BACKGROUND"]:
        parts.append("=== BACKGROUND — Stable, reference only if contradicts thesis ===")
        parts.extend(groups["BACKGROUND"])
        parts.append("")

    return "\n".join(parts)


def build_shared_briefing_parts(today, market_regime, top5_text, signal_summary,
                                 signal_conflicts, macro_text, context, signal_data,
                                 quant_context, chart_data, options_data,
                                 past_overviews_text, asset_priority):
    """Build data parts common to all reports (free-tier and per-user).

    Returns a list of string parts that can be joined into a briefing.
    These parts contain ALL shared market data: macro research, signals,
    quant engine, chart data, derivatives, sentiment. User-specific context
    (spotlight selection, positions, plays, indicator focus) is added later
    by build_user_briefing() or the free-tier report path.
    """
    # Data availability header
    avail_status = context.get("_availability", {})
    avail_lines = []
    ok_sources = [k for k, v in avail_status.items() if v == "ok"]
    fail_sources = [k for k, v in avail_status.items() if v != "ok"]
    if avail_status:
        avail_lines.append("=== DATA AVAILABILITY STATUS ===")
        if ok_sources:
            avail_lines.append(f"AVAILABLE: {', '.join(ok_sources)}")
        if fail_sources:
            avail_lines.append(f"UNAVAILABLE: {', '.join(fail_sources)}")
            avail_lines.append("Apply degradation rules for UNAVAILABLE sources.")
        avail_lines.append("================================\n")
    avail_header = "\n".join(avail_lines)

    # Signal conflicts
    conflict_text = ""
    if signal_conflicts:
        conflict_lines = ["## Signal Conflicts (resolve in report)"]
        for c in signal_conflicts:
            conflict_lines.append(f"- {c['pair'][0]} vs {c['pair'][1]}")
            conflict_lines.append(f"  Implication: {c['implication']}")
            conflict_lines.append(f"  Resolution: {c['resolution']}")
        conflict_text = "\n".join(conflict_lines)

    parts = [
        avail_header,
        f"# Morning Briefing — {today}\n",
        f"## Risk Posture: {market_regime.upper()}\n",
        # Top-5 drivers
        f"## Today's Top-5 Market Drivers\n{top5_text}\n" if top5_text else "",
        # Signal summary
        f"## Pre-Processed Signal Summary\n```json\n{signal_summary}\n```\n",
        # Signal conflicts
        f"\n{conflict_text}\n" if conflict_text else "",
        # Macro research
        f"## Macro Research\n{macro_text}\n",
        # Sentiment
        build_sentiment_with_priority(context.get('sentiment', 'Unavailable'),
                                      signal_data.get('onchain_signals', {})),
        f"## Sentiment Changes\n{context.get('sentiment_history', 'Unavailable')}\n",
        f"## Historical Context\n{context.get('history_context', 'Unavailable')}\n",
        # Signal changes
        f"## Signal Changes\n{context.get('signal_diff', 'Unavailable')}\n",
        f"## Relative Strength\n{context.get('rs_rankings', 'Unavailable')}\n",
        # Quant Engine
        f"## Quant Engine Context\n{quant_context.get('quant', 'Unavailable')}\n",
        # Chart data (prioritized)
        f"\n{build_prioritized_chart_data(chart_data, asset_priority) if asset_priority else chart_data}\n",
        # Options + derivatives
        f"\n{options_data}\n",
        f"\n## Derivatives Regime Playbook Reference\n{load_derivatives_regimes()}\n" if options_data and 'unavailable' not in options_data.lower() else "",
        # Past overviews for continuity
        f"## Recent Overview History (past 5 days)\n{past_overviews_text}\n" if past_overviews_text else "",
    ]
    return [p for p in parts if p]
