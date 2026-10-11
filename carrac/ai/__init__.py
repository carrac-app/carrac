"""AI package — a LIVE shared library, not a parked feature.

Read this before deleting anything here: the *Copilot chat surface* is
flag-off, the *package* is not. Fourteen production modules outside this
package import from it, and several of those paths are reachable by users
today:

  * Journal AI review  — carrac/journal/ai_review.py,
                          carrac/fastapi_app/routers/journal_ai.py
                          (tier-gated only; NO chat-flag check)
  * MCP server         — carrac/mcp/primitives.py, mcp/tools/{recipes,journal}.py
  * LLM pricing tables — carrac/llm/{anthropic,grok,morning_anthropic}.py
  * Spend accounting   — carrac/reports/routes.py,
                          morning_engine/{engine,dashboard_api}.py
  * Admin health       — carrac/admin/health_checks.py
  * Advice grounding   — carrac/facts/sentences.py (BANNED_ADVICE_PHRASES)
  * /api/ai/trading-profile — carrac/ai/routes.py, called unconditionally
                          from static/js/contextual-nudges.js

Only the chat surface is gated, at three checkpoints that all read one flag
(``is_ai_chat_enabled_flag()``, PostHog ``ai_chat_enabled``, fail-closed
OFF — carrac/integrations.py):

  * carrac/dashboard/routes.py           (UI render)
  * carrac/fastapi_app/routers/ai.py     (chat endpoint)
  * carrac/fastapi_app/routers/ai_aux.py (chat aux endpoint)

Two siblings are deliberately NOT on that flag: ``ai_weekly_review``
(tier-gated) and ``ai_context_preview`` (login-only, never calls an LLM).

The chat surface stays off until a credit system exists — that is a product
decision, not neglect.
"""

from carrac.ai.routes import ai_bp

__all__ = ['ai_bp']
