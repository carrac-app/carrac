"""Deduplication checker for morning engine reports.

Two checks:
  1. Intra-report: sentences of 8+ words appearing near-verbatim in more than
     one section, and numeric facts (same number-token) appearing in 3+ sections.
  2. Cross-report: share of general-report sentences appearing in the operator
     morning report (detects paraphrasing of the morning report).

Thresholds (configurable via kwargs or constants):
  INTRA_DUP_SENTENCE_THRESHOLD  = 2   pairs of duplicated sentences → retry
  INTRA_DUP_FACT_THRESHOLD       = 3   sections containing the same number → retry
  CROSS_REPORT_OVERLAP_THRESHOLD = 0.25  fraction → retry

All functions are pure (no I/O, no imports of carrac modules) so they are
fast and easily unit-tested without conftest gymnastics.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
INTRA_DUP_SENTENCE_THRESHOLD: int = 2    # number of dup-sentence PAIRS before retry
INTRA_DUP_FACT_THRESHOLD: int = 3        # number of SECTIONS containing same number
CROSS_REPORT_OVERLAP_THRESHOLD: float = 0.25  # fraction of general sentences in morning


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip markdown noise, strip punctuation."""
    # Strip markdown: **, *, #, >, `, |
    text = re.sub(r'[*#>`|_~]+', ' ', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)
    return text.strip().lower()


def _tokenise_sentences(text: str) -> list[str]:
    """Split text into sentences of 8+ words, normalised."""
    # Split on sentence-ending punctuation followed by whitespace or EOL
    raw = re.split(r'(?<=[.!?])\s+', text)
    result = []
    for sent in raw:
        norm = _normalise(sent)
        if len(norm.split()) >= 8:
            result.append(norm)
    return result


def _extract_numbers(text: str) -> set[str]:
    """Return the set of numeric tokens in text (integers + simple decimals)."""
    return set(re.findall(r'\b\d[\d,]*(?:\.\d+)?\b', text))


def _parse_sections(report: str) -> dict[str, str]:
    """Split a markdown report on ## headings into {heading: body} dict."""
    sections: dict[str, str] = {}
    parts = re.split(r'^## ', report, flags=re.MULTILINE)
    for part in parts:
        if not part.strip():
            continue
        lines = part.split('\n', 1)
        heading = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ''
        sections[heading] = body
    return sections


# ---------------------------------------------------------------------------
# Intra-report deduplication
# ---------------------------------------------------------------------------

def check_intra_report(report: str) -> dict[str, Any]:
    """Check a single report for internal sentence and fact duplication.

    Returns a dict with:
      - 'duplicate_sentence_pairs': list of (sent, [section_names]) for each dup
      - 'duplicate_fact_sections': list of (number, [section_names]) for facts
        appearing in 3+ sections
      - 'exceeds_threshold': bool — True if either threshold is breached
      - 'dup_pair_count': int
      - 'dup_fact_count': int (facts appearing in INTRA_DUP_FACT_THRESHOLD+ sections)
    """
    sections = _parse_sections(report)

    # --- Sentence dedup ---
    # Build map: normalised_sentence -> list of section headings that contain it
    sent_to_sections: dict[str, list[str]] = {}
    for heading, body in sections.items():
        for sent in _tokenise_sentences(body):
            sent_to_sections.setdefault(sent, []).append(heading)

    dup_sentence_pairs: list[tuple[str, list[str]]] = [
        (sent, sec_list)
        for sent, sec_list in sent_to_sections.items()
        if len(sec_list) > 1
    ]

    # --- Numeric fact dedup ---
    # Build map: number_token -> set of sections containing it
    number_to_sections: dict[str, set[str]] = {}
    for heading, body in sections.items():
        for num in _extract_numbers(body):
            number_to_sections.setdefault(num, set()).add(heading)

    # Only flag numbers that appear in 3+ sections AND that are "interesting"
    # (not trivially short like single digits 0-9)
    dup_fact_sections: list[tuple[str, list[str]]] = [
        (num, sorted(sec_set))
        for num, sec_set in number_to_sections.items()
        if len(sec_set) >= INTRA_DUP_FACT_THRESHOLD and (
            len(num) >= 3 or (len(num) >= 2 and '.' in num)
        )
    ]

    dup_pair_count = len(dup_sentence_pairs)
    dup_fact_count = len(dup_fact_sections)

    exceeds = (
        dup_pair_count >= INTRA_DUP_SENTENCE_THRESHOLD
        or dup_fact_count > 0
    )

    return {
        'duplicate_sentence_pairs': dup_sentence_pairs,
        'duplicate_fact_sections': dup_fact_sections,
        'dup_pair_count': dup_pair_count,
        'dup_fact_count': dup_fact_count,
        'exceeds_threshold': exceeds,
    }


# ---------------------------------------------------------------------------
# Cross-report overlap
# ---------------------------------------------------------------------------

def check_cross_report_overlap(morning_report: str, general_report: str) -> dict[str, Any]:
    """Compute the fraction of general-report sentences that appear verbatim
    in the morning report.

    Returns a dict with:
      - 'overlap_fraction': float in [0, 1]
      - 'overlapping_sentences': list of sentences found in both
      - 'exceeds_threshold': bool
      - 'general_sentence_count': int
    """
    morning_sents = set(_tokenise_sentences(morning_report))
    general_sents = _tokenise_sentences(general_report)

    if not general_sents:
        return {
            'overlap_fraction': 0.0,
            'overlapping_sentences': [],
            'exceeds_threshold': False,
            'general_sentence_count': 0,
        }

    overlapping = [s for s in general_sents if s in morning_sents]
    fraction = len(overlapping) / len(general_sents)

    return {
        'overlap_fraction': round(fraction, 4),
        'overlapping_sentences': overlapping,
        'exceeds_threshold': fraction > CROSS_REPORT_OVERLAP_THRESHOLD,
        'general_sentence_count': len(general_sents),
    }


# ---------------------------------------------------------------------------
# Corrective-retry instruction builder
# ---------------------------------------------------------------------------

def build_dedup_correction_prompt(report: str, intra_result: dict[str, Any],
                                  cross_result: dict[str, Any] | None = None) -> str:
    """Build a concise instruction string for the corrective LLM retry.

    Includes up to 5 examples of each violation type to stay within token budgets.
    """
    lines = [
        "The draft below has duplication issues. Rewrite it, eliminating the "
        "specific duplicates listed — keep all other content and length.",
        "",
        "DUPLICATION ISSUES TO FIX:",
    ]

    # Sentence duplicates
    dup_pairs = intra_result.get('duplicate_sentence_pairs', [])
    if dup_pairs:
        lines.append(f"\n{len(dup_pairs)} sentence(s) appear in multiple sections "
                     f"(keep it in the FIRST section only, remove later occurrences):")
        for sent, sec_list in dup_pairs[:5]:
            # Show a truncated version for readability
            preview = sent[:120] + '...' if len(sent) > 120 else sent
            lines.append(f"  - \"{preview}\" (sections: {', '.join(sec_list)})")

    # Fact duplicates
    dup_facts = intra_result.get('duplicate_fact_sections', [])
    if dup_facts:
        lines.append(f"\n{len(dup_facts)} number(s) appear in 3+ sections "
                     f"(each number belongs in ONE home section; later sections may "
                     f"reference the CONCLUSION, not restate the number):")
        for num, sec_list in dup_facts[:5]:
            lines.append(f"  - {num!r} (sections: {', '.join(sec_list)})")

    # Cross-report overlap
    if cross_result and cross_result.get('exceeds_threshold'):
        frac = cross_result.get('overlap_fraction', 0)
        lines.append(
            f"\nCross-report overlap: {frac:.0%} of sentences appear verbatim in the "
            f"operator morning report. This is a standalone publication for a different "
            f"audience — rewrite in a fresh voice. Do NOT rephrase the morning report; "
            f"draw independently from the underlying data."
        )
        for sent in (cross_result.get('overlapping_sentences') or [])[:3]:
            preview = sent[:120] + '...' if len(sent) > 120 else sent
            lines.append(f"  Overlapping: \"{preview}\"")

    lines += [
        "",
        "REPORT TO REWRITE:",
        "",
        report,
    ]
    return "\n".join(lines)
