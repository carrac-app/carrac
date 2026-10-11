"""Post-generation fact verifier using Claude Haiku.

Wired into the report-generation surfaces in morning_engine.engine
via the shared `_verify_tripwire()` helper: the general/shared report
(run_dispatch, and _generate_general_report which both run_dispatch and
run_morning call), run_afternoon, run_weekly, run_week_ahead (both the shared
region report and the per-user personal section), and the per-user
personalized-report path (_run_personalized_reports — gated behind
PERSONALIZED_REPORTS, which stays off in production, so this call site exists but
does not fire there yet). Every call site runs the verifier AFTER the
existing regex tripwires and (where one exists) the blocking publish gate.
It is NOT yet wired into the audio-briefing scripts, AI Copilot, or trade
review; wiring those is still open.

Haiku is ~5x cheaper than Sonnet and fast enough (<3s typical) to run inline.

Not a gate by itself — it returns a VerifyResult with a verdict; the caller
decides whether to regenerate, annotate, or (the current policy for every
wired surface, per the shared-report
asymmetry) log + notify the operator and otherwise leave the report as-is.
Blocking a shared report on a second opinion would leave every user with no
report at all, which is worse than shipping one flagged claim.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from carrac.ai.copy import with_advice_ban

log = logging.getLogger("carrac.grounding.verify")

# Warn once at import if fact verification is disabled so an
# accidental GROUNDING_VERIFY_ENABLED=0 is visible in logs. Does NOT change behavior.
if os.environ.get("GROUNDING_VERIFY_ENABLED", "1") == "0":
    log.critical(
        "GROUNDING_VERIFY_ENABLED=0 — post-generation fact verification is DISABLED. "
        "verify_against_source() will return CLEAN unconditionally. "
        "Set GROUNDING_VERIFY_ENABLED=1 to re-enable."
    )

_VERIFY_MODEL = os.environ.get("GROUNDING_VERIFY_MODEL", "claude-haiku-4-5")

# Haiku verifier wiring — character caps for the (source, output) pair sent to Haiku.
#
# The caps must be sized against real report sizes; small caps (e.g. source
# 16000, generated 6000) fail in three ways:
#
#   - Real reports run 11,600-16,800 chars for the LLM-authored narrative
#     alone (measured off 5 `general` reports), so a 6000-char output cap
#     would leave the back ~55-65% of every real report silently unchecked.
#   - The assembled SOURCE briefing these reports are generated from is far
#     bigger than 16000 chars: macro research alone is capped at
#     MAX_MACRO_TOKENS=4000 tokens (~16000 chars) and routinely uses most of
#     that, i.e. macro research alone can consume a 16000-char source cap
#     before the LLM's own chart data, quant-engine context, sentiment,
#     RS rankings, or calendar text are ever added. run_weekly's briefing is
#     worse: it embeds up to 5 recent reports at 8000 chars each (40000
#     chars) plus its own narrative arc and chart data on top.
#   - Consequence: a claim about (say) the RS-rankings table or a chart
#     level would be checked against a source blob that never contained the
#     RS rankings or that chart's data — a FALSE "HALLUCINATION", not a
#     real one. A verifier that cries wolf gets ignored and its signal
#     becomes noise; a false-positive-prone verifier here is worse than no
#     verifier.
#
# Haiku's context window is 200K tokens (~800K chars at the usual ~4
# chars/token for English). Even the new, much larger caps below use well
# under 15% of that, so there is enormous headroom and no need for
# section-by-section chunking: raising the caps is enough. At these caps a
# typical verify call (see the real-report measurements above) costs about
# $0.02-0.05 — see the wiring in morning_engine.engine._verify_tripwire.
_MAX_SOURCE_CHARS = 100_000
_MAX_GENERATED_CHARS = 20_000

# The OUTPUT cap must be sized against a real HALLUCINATION verdict, not a
# CLEAN one: a CLEAN reply is ~28 tokens, but the verdict schema requires
# every unsupported claim to carry a quote AND a why, so a report with a
# handful of flagged claims blows past a small cap (e.g. 600) mid-JSON. The
# truncated reply then fails json.loads and is filed as a generic ERROR at
# debug level — i.e. the verifier goes silent EXACTLY when it found
# something.
#
# A bigger source naturally produces more flagged claims when something is
# off, so the per-claim cost scales with source size. The `weekly` report
# (which embeds up to 5 recent reports at 8000 chars each, see
# _MAX_SOURCE_CHARS above) is the largest case: 4000 tokens was not enough
# and its verdict was discarded as <truncated>, so it shipped unchecked.
# 8000 leaves room for ~50 fully-quoted claims; anything past that is a
# report so broken the count no longer matters. Haiku output is $5/MTok, so
# the worst case is ~$0.04 per call, which is not real money at this volume.
_MAX_VERDICT_TOKENS = 8_000

_VERIFY_SYSTEM = """\
You are a factual consistency auditor. Given a SOURCE (a structured market report and a facts block) and an OUTPUT (a derived summary or audio script), list every factual claim in the OUTPUT that is NOT supported by the SOURCE.

Rules:
- Only flag concrete claims: numbers, dates, named events, policy decisions, specific price levels.
- Do NOT flag tone, style, or opinion/analysis.
- If the OUTPUT says something the SOURCE does not explicitly say and could not be inferred from it, flag it.
- Training-data knowledge from YOUR memory does NOT count as support — only the SOURCE text counts.

Return STRICT JSON only, no prose:
{"verdict":"CLEAN"|"HALLUCINATION","unsupported":[{"quote":"…","why":"…"}]}
"""
# Every model prompt carries the non-advice ban (tests/test_prompt_advice_ban.py);
# the auditor note keeps it free to quote the text it is checking.
_VERIFY_SYSTEM = with_advice_ban(_VERIFY_SYSTEM, auditor=True)


@dataclass
class VerifyResult:
    verdict: str  # "CLEAN" | "HALLUCINATION" | "ERROR"
    unsupported: List[Dict[str, str]] = field(default_factory=list)
    cost: float = 0.0
    raw: str = ""

    @property
    def clean(self) -> bool:
        return self.verdict == "CLEAN"


def verify_against_source(generated_text: str,
                           source_text: str,
                           facts_block_text: str = "",
                           *,
                           anthropic_client: Optional[Any] = None) -> VerifyResult:
    """Run a Haiku pass over (source, facts, output) returning a verdict.

    anthropic_client: an instance with .chat(system_prompt, user_message)
    returning {content, usage}. If not provided, builds a MorningAnthropicClient.
    Respects GROUNDING_VERIFY_ENABLED=0 for kill-switch.
    """
    if os.environ.get("GROUNDING_VERIFY_ENABLED", "1") == "0":
        return VerifyResult(verdict="CLEAN", raw="<disabled>")

    if not generated_text:
        return VerifyResult(verdict="CLEAN", raw="<empty>")

    client = anthropic_client
    if client is None:
        try:
            from morning_engine.anthropic_client import MorningAnthropicClient
            client = MorningAnthropicClient(model=_VERIFY_MODEL)
        except Exception as e:
            log.warning("Verify client init failed: %s", e)
            return VerifyResult(verdict="ERROR", raw=str(e))

    user_msg = (
        f"=== SOURCE FACTS BLOCK ===\n{facts_block_text}\n\n"
        f"=== SOURCE REPORT ===\n{source_text[:_MAX_SOURCE_CHARS]}\n\n"
        f"=== OUTPUT TO VERIFY ===\n{generated_text[:_MAX_GENERATED_CHARS]}"
    )

    try:
        resp = client.chat(
            system_prompt=_VERIFY_SYSTEM,
            user_message=user_msg,
            max_tokens=_MAX_VERDICT_TOKENS,
            temperature=0.0,
        )
        raw = resp.get("content", "").strip()
        cost = (resp.get("usage") or {}).get("cost", 0.0)
        stop_reason = resp.get("stop_reason")
    except Exception as e:
        log.warning("Verify call failed: %s", e)
        return VerifyResult(verdict="ERROR", raw=str(e))

    # A truncated verdict is not a parse error, it is a BLIND
    # verifier — and it truncates precisely when it has the most to say (a
    # CLEAN verdict is ~28 tokens; only an enumeration of unsupported claims
    # can run long). Name it explicitly so the caller can alarm on it rather
    # than filing it under the same silent ERROR bucket as a malformed reply.
    if stop_reason == "max_tokens":
        log.error(
            "[grounding] verifier response TRUNCATED at max_tokens=%d — verdict "
            "discarded. The report was NOT checked. Raise _MAX_VERDICT_TOKENS.",
            _MAX_VERDICT_TOKENS,
        )
        return VerifyResult(verdict="ERROR", raw=f"<truncated>{raw[:400]}",
                            cost=cost)

    # Be tolerant: strip code fences, find first JSON object
    cleaned = raw
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(0)
    try:
        parsed = json.loads(cleaned)
        verdict = str(parsed.get("verdict", "ERROR")).upper()
        if verdict not in ("CLEAN", "HALLUCINATION"):
            verdict = "ERROR"
        unsupported = parsed.get("unsupported") or []
        return VerifyResult(verdict=verdict, unsupported=unsupported, cost=cost, raw=raw)
    except Exception as e:
        log.warning("Verify JSON parse failed: %s (raw=%s)", e, raw[:200])
        return VerifyResult(verdict="ERROR", raw=raw, cost=cost)
