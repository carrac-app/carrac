"""Strip operator-private sections from a shared report before it can reach a
free-tier consumer.

The free tier is served the operator-neutral ``general`` report, never the
``morning`` report (the morning report bakes in the operator's own positions /
alerts — see ``morning_engine/prompts.py``). The serving layer must not fall
back to the ``morning`` report when no ``general`` row exists yet; that
fallback is the leak path. This sanitizer is the defense-in-depth backstop: any code path that still surfaces a
``morning`` report to a non-paying context runs the content through here first,
so an accidental fallback (a code regression, a fresh DB, a pruned ``general`` row)
can never expose the operator's positions. ``report_text_for_viewer`` at the
bottom is the one policy the serving routes apply.

Pure stdlib (``re``) so it's safe to import from both the Flask serving routes
and the MCP tool layer without dragging in heavy dependencies.
"""

import re

# Sections of the shared morning/afternoon reports that carry the OPERATOR's
# own alerts / positions and must never reach a non-operator consumer. "Action
# Dashboard" is the live position checklist; "Updated Action Items" is its
# afternoon-delta sibling; the others are legacy / variant section names that
# have historically held the same operator-private context.
FREE_TIER_UNSAFE_SECTIONS = (
    "Action Dashboard",
    "Action Items",
    "Updated Action Items",
    "Alerts & Positions Follow-Up",
    # Weekly digest sections that review the OPERATOR's own plays/trades.
    "Play Review",
    "What Worked / What Didn't",
)


def _strip_section(text, section_name):
    """Remove a single ``## Section`` block (heading + body) from markdown.

    Tolerates numbered headings (the afternoon report numbers its sections:
    ``## 5. Updated Action Items``).
    """
    pattern = rf"^## (?:\d+\.\s*)?{re.escape(section_name)}\s*\n.*?(?=^## |\Z)"
    return re.sub(pattern, "", text, count=1, flags=re.MULTILINE | re.DOTALL)


def strip_operator_sections(content):
    """Return ``content`` with all operator-private sections removed.

    Idempotent and safe on operator-neutral reports (the ``general`` report is
    only "The Story" + "Market Snapshot", so this is a no-op there). Falsy input
    (``None`` / ``""``) is returned unchanged.
    """
    if not content:
        return content
    out = content
    for name in FREE_TIER_UNSAFE_SECTIONS:
        out = _strip_section(out, name)
    return out


# Line-level backstop: the engine is INSTRUCTED to weave position context into
# prose sections too ("The Story owns ... position-level context" —
# morning_engine/prompts.py), so section-stripping alone cannot guarantee an
# operator-neutral artifact. These fingerprints match personal-trade phrasing,
# not market commentary; over-matching a benign line is acceptable, leaking an
# entry/stop is not.
_POSITION_LINE_RE = re.compile(
    r"(?i)("
    # A pattern of `entr(?:y|ies) \$` (dollar sign IMMEDIATELY after the
    # word) would cut "entered at $2,251" but let "Entry at $2,251" through.
    # Real line from a morning report:
    #   "**ETH Long Position Deep Underwater**: Entry at $2,251, current $1,630"
    # So `at`/`:` are optional connectors. Still anchored on the dollar sign,
    # which is what keeps "the entry level for the pattern" out of it — this
    # filter drops WHOLE lines, so over-matching deletes real commentary.
    r"entered at \$|entr(?:y|ies)\s*(?:at\s+|:\s*)?\$|with stop \$|stops? \$\d|"
    r"in-the-money|active (?:short|long) position|"
    r"(?:short|long) entered|position severely|open position"
    r")"
)


def scrub_position_lines(content):
    """Drop whole lines that carry operator-trade fingerprints."""
    if not content:
        return content
    kept = [ln for ln in content.splitlines() if not _POSITION_LINE_RE.search(ln)]
    return "\n".join(kept)


def _scrub_licensed(content):
    """Drop Deribit-derived prose. Lazy import — the gate module
    pulls in the quant registry, and this file is imported by the MCP layer."""
    from carrac.compliance.deribit_gate import scrub_deribit_prose
    return scrub_deribit_prose(content)


def public_x_candidates(candidates):
    """Filter stored X-post candidates to those safe to publish.

    The candidates live in the report's ``metadata``, not its ``content``, so
    ``general_report_public`` never saw them — 27 of 193 stored candidates
    narrate DVOL/GEX, and they are both exposed on the non-operator API path
    and taken verbatim as tweet captions by carrac/social/daily_card.py.

    DROPS rather than scrubs. A tweet is one 280-character claim; removing its
    barred sentence usually leaves a fragment, and a mutilated tweet is worse
    than one fewer candidate. Non-strings and blanks are dropped too, so
    callers can hand the raw metadata list straight in.
    """
    from carrac.compliance.deribit_gate import scrub_deribit_prose

    out = []
    for c in (candidates or []):
        if not isinstance(c, str) or not c.strip():
            continue
        if (scrub_deribit_prose(c) or '').strip() != c.strip():
            continue  # something had to be removed -> not publishable
        out.append(c)
    return out


def general_report_public(content):
    """The serving filter for the operator-neutral ``general`` report.

    The ``general`` report needs no position scrub (it is operator-neutral by
    construction, and ``scrub_position_lines`` would over-match its market
    commentary) but it DOES need the licensing scrub: 56 of the 65 stored
    reports were written while the Deribit block was in the briefing.

    Every path serving a stored ``general`` report calls this — ``/brief``,
    ``/brief/<date>`` and the FastAPI report router. Adding a new one without
    it reopens the Deribit-prose leak.
    """
    return _scrub_licensed(strip_operator_sections(content))


def operator_neutral(content):
    """The full non-operator serving filter for morning/afternoon content:
    section strip + position-line scrub + licensing scrub. Do NOT apply to the
    ``general`` report — use ``general_report_public``; the position scrub
    could over-match legitimate market commentary there.

    The licensing scrub is here as well as in ``general_report_public`` because
    the morning/afternoon reports narrate the same options data, and the MCP
    report tools serve them to agents."""
    return _scrub_licensed(scrub_position_lines(strip_operator_sections(content)))


def report_text_for_viewer(report, report_type, *, operator):
    """The text fields of a stored report as one viewer may see them.

    This is the serving policy. Every handler that returns report text or
    narration calls it and returns ``content``, ``summary`` and
    ``audio_script`` from the result, never from the row.

    ``operator`` (the admin tier, including the internal key the report engine
    uses) gets the row unchanged. Everyone else gets:

    - ``content``: ``general_report_public`` for the ``general`` report,
      ``operator_neutral`` for every other type.
    - ``summary``: withheld for ``morning``, where it is the operator's
      personal executive summary; otherwise the same filter as ``content``.
    - ``audio_script``: withheld. Every stored narration is written from the
      operator's full text (the morning one reads the Action Dashboard, the
      afternoon one the raw report), and spoken prose cannot be filtered
      section by section after the fact. The mp3 spoken from it is withheld
      for the same reason.
    """
    content = report.get('content')
    summary = report.get('summary')
    if operator:
        return {'content': content, 'summary': summary,
                'audio_script': report.get('audio_script')}
    public = general_report_public if report_type == 'general' else operator_neutral
    return {
        'content': public(content),
        'summary': None if report_type == 'morning' else public(summary),
        'audio_script': None,
    }
