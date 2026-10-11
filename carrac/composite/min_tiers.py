"""Single source of truth for the composite-route tier bars.

Three surfaces serve the composite data and every one must advertise or
enforce the SAME bar, so the bar is declared once, here:

* OpenAPI document — carrac/openapi/paths/composite.py, via ``x-min-tier``
* FastAPI router   — carrac/fastapi_app/routers/composite.py (the LIVE
  handlers; carrac/asgi.py routes all three paths to FastAPI), via
  ``require_min_tier(...)``
* MCP twins        — gated independently by carrac/mcp/protocol.py
  (``tool_entitled`` runs before every dispatch); the bars here are
  deliberately aligned with those registrations:

      asset detail    ↔ market_get_detail    min_tier='free'
      live positions  ↔ trade_get_positions  min_tier='free'
      batch           ↔ util_batch_query     min_tier='member'

'free' is a real decision, not a missing gate: the web details drawer
(static/js/details-drawer.js, loaded for every logged-in user) calls
/api/asset/<sym>/detail, and /api/positions/live returns the caller's own
trades. 'member' on batch closes a leak — a free account
pulling uncapped scanner scores for 10 symbols per call, bypassing
FREE_SCANNER_RESULT_LIMIT.

Keep this module import-free at load time — the FastAPI router imports it
at module load and must stay cold-start cheap.
"""

ASSET_DETAIL_MIN_TIER = 'free'
LIVE_POSITIONS_MIN_TIER = 'free'
BATCH_MIN_TIER = 'member'


def min_tier_denial_payload(current_tier, min_tier):
    """403 body for a min_tier refusal — same shape as ``tier_required``
    (carrac/auth/decorators.py) and FastAPI's ``require_tier``
    (carrac/fastapi_app/deps/auth.py), so clients see ONE tier-denial
    idiom regardless of which gate refused them.
    """
    from carrac.mcp.registry import TIER_RANK  # single tier-rank source

    bar = TIER_RANK.get(min_tier or 'free', 0)
    required = sorted(
        (t for t, rank in TIER_RANK.items() if rank >= bar),
        key=lambda t: (TIER_RANK[t], t),
    )
    return {
        'error': 'Subscription required',
        'upgrade_required': True,
        'current_tier': current_tier,
        'required_tiers': required,
    }
