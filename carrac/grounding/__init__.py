"""Anti-hallucination grounding infrastructure.

Shared across morning engine, AI Copilot, and journal AI review.
Guards against training-data bleed: an LLM restating stale facts from its
training data (e.g. an out-of-date Fed rate decision) instead of the live ones.

Public surface:
- facts_block() — ground-truth text injected into every LLM system prompt
- preamble() — hard-rule preamble prepended to every prompt
- hallucination_scan.scan() — regex tripwire for training-era patterns
- verify.verify_against_source() — optional Haiku post-gen fact check

All helpers honor the GROUNDING_ENABLED env flag (default: on). Setting
GROUNDING_ENABLED=0 disables the tripwire and bypasses verification,
but preamble/facts_block are always injected for backward compatibility.
"""

from carrac.grounding.facts_block import facts_block, build_facts_dict, render_macro_facts_strip
from carrac.grounding.preamble import preamble, grounded_system_prompt
from carrac.grounding.hallucination_scan import scan, has_high, filter_severity, Finding, Severity
from carrac.grounding.escape import escape_user_text, escape_user_dict_field, neutralize_inline

__all__ = [
    "facts_block",
    "build_facts_dict",
    "render_macro_facts_strip",
    "preamble",
    "grounded_system_prompt",
    "scan",
    "has_high",
    "filter_severity",
    "Finding",
    "Severity",
    "escape_user_text",
    "escape_user_dict_field",
    "neutralize_inline",
]
