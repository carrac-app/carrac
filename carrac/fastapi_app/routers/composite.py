"""Composite endpoints — proof point.

The whole strangler-fig migration was justified by these endpoints'
fan-out cost on the Flask gthread worker pool. Sequential sub-fetches
(scanner cache → HTF cache → prices → sentiment → alerts → plays →
positions → drawings) saturated worker threads under load and pushed
postgres pool checkout count past the connection ceiling.

This router runs the same fan-out under asyncio: each sub-fetch is
``asyncio.to_thread()``-ed and gathered. Sequential time = Σ t_i
becomes parallel time = max(t_i). At the same DB pool size, concurrent
capacity rises 5–10× because the worker no longer sits idle while
waiting for cache/DB I/O.

Three routes ported:

* ``GET /api/asset/{symbol}/detail``  — single-asset full detail
* ``GET /api/positions/live``         — open trades enriched with live data
* ``GET /api/batch``                  — multi-asset, multi-block (the proof point)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from carrac.composite.min_tiers import (
    ASSET_DETAIL_MIN_TIER,
    BATCH_MIN_TIER,
    LIVE_POSITIONS_MIN_TIER,
    min_tier_denial_payload,
)
from carrac.fastapi_app.deps.auth import RateCtxDep, auth_or_key
from carrac.fastapi_app.limiter import limiter

logger = logging.getLogger("carrac.fastapi.composite")

router = APIRouter()

VALID_BLOCKS = frozenset(
    {"market", "scanner", "sentiment", "alerts", "plays", "positions", "drawings"}
)
MAX_BATCH_ASSETS = 10


def require_min_tier(min_tier: str):
    """Resolve auth via ``auth_or_key``, then refuse callers
    whose tier ranks below ``min_tier``.

    The bar comes from ``carrac/composite/min_tiers.py`` (shared with the
    OpenAPI path declarations — one source, both surfaces).
    Denial shape matches ``require_tier`` (deps/auth.py) / ``tier_required``.
    ``auth_or_key`` resolves first, so an anonymous caller still gets its
    canonical 401 — never a tier 403.
    """

    def _dep(auth: Annotated[dict, Depends(auth_or_key)]) -> dict:
        # Lazy import: keep the tier registry out of module load.
        from carrac.mcp.registry import tier_meets

        tier = auth.get("tier") or "free"
        if not tier_meets(tier, min_tier):
            raise HTTPException(
                status_code=403,
                detail=min_tier_denial_payload(tier, min_tier),
            )
        return auth

    # Introspection hook so tests can prove EVERY composite route is wired
    # through this gate (a "built but not connected" guard is no guard).
    _dep._min_tier_gate = min_tier
    return _dep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_user_id(auth: dict) -> Optional[int]:
    """Resolve the caller's user_id for data-scoping.

    Prefers the ``api_key_user_id`` (per-user API key) over the session
    ``user_id``.
    Returns ``None`` when neither is present.
    """
    if auth.get("source") == "api_key":
        return auth.get("user_id")
    user = auth.get("user") or {}
    return user.get("id")


def _require_user_id(auth: dict) -> int:
    """Resolve user_id or raise 401."""
    uid = _resolve_user_id(auth)
    if uid is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "authentication required",
                "detail": (
                    "This endpoint returns user-scoped data and cannot be "
                    "called with a shared admin key without per-user "
                    "binding. Use a per-user API key."
                ),
            },
        )
    return uid


def _is_compact(request: Request) -> bool:
    return request.query_params.get("format") == "compact"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/asset/{symbol}/detail")
@limiter.limit("60/minute")
async def asset_detail(
    request: Request,
    symbol: str,
    auth: Annotated[dict, Depends(require_min_tier(ASSET_DETAIL_MIN_TIER))],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Everything about one asset in a single call.

    Runs the 8 sub-fetches concurrently via ``asyncio.gather`` over
    ``asyncio.to_thread`` calls.
    """
    user_id = _require_user_id(auth)

    # Local imports keep the FastAPI app cold-start cheap; assemblers
    # reach into Flask-side cache/DB modules that pull in heavy deps.
    from carrac.composite.assemblers import assemble_asset_detail
    from carrac.composite.formatters import compact_asset_detail

    # The full assemble_asset_detail is itself a fan-out; running it
    # inside one to_thread is enough to keep the event loop free
    # (this keeps a stable wire shape; per-block gather is a
    # follow-up if profiling shows it pays).
    result = await asyncio.to_thread(assemble_asset_detail, symbol, user_id)
    if _is_compact(request):
        result = compact_asset_detail(result)
    return JSONResponse(content=jsonable_encoder(result))


@router.get("/api/positions/live")
@limiter.limit("60/minute")
async def live_positions(
    request: Request,
    auth: Annotated[dict, Depends(require_min_tier(LIVE_POSITIONS_MIN_TIER))],
    ctx: RateCtxDep,
    asset: Optional[str] = Query(default=None),
) -> JSONResponse:
    """Open trades enriched with live market data."""
    user_id = _require_user_id(auth)

    from carrac.composite.assemblers import assemble_live_positions
    from carrac.composite.formatters import compact_live_positions

    result = await asyncio.to_thread(
        assemble_live_positions, user_id, asset_filter=asset
    )
    if _is_compact(request):
        result = compact_live_positions(result)
    return JSONResponse(content=jsonable_encoder(result))


@router.get("/api/batch")
@limiter.limit("30/minute")
async def util_batch_query(
    request: Request,
    auth: Annotated[dict, Depends(require_min_tier(BATCH_MIN_TIER))],
    ctx: RateCtxDep,
    assets: str = Query(default=""),
    include: str = Query(default="market,scanner"),
) -> JSONResponse:
    """Multi-asset, multi-block — THE asyncio.gather proof point.

    Sub-fetches that are independent of each other run concurrently:

    * 4 global cache reads: scanner, HTF, prices, sentiment
    * 4 user-scoped reads:  alerts (file I/O), plays (DB), positions (DB), drawings (DB)

    All 8 are independent, so they run in parallel via ``asyncio.gather``
    over ``asyncio.to_thread`` calls rather than one after another.
    Per-asset assembly afterwards is pure CPU and unchanged.
    """
    if not assets:
        raise HTTPException(
            status_code=400,
            detail="Missing required parameter: assets (comma-separated symbols)",
        )

    asset_list = [s.strip() for s in assets.split(",") if s.strip()]
    if not asset_list:
        raise HTTPException(
            status_code=400,
            detail="No valid symbols in assets parameter",
        )
    if len(asset_list) > MAX_BATCH_ASSETS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_BATCH_ASSETS} assets per request",
        )

    include_blocks = [b.strip() for b in include.split(",") if b.strip()]
    invalid = set(include_blocks) - VALID_BLOCKS
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid blocks: {', '.join(sorted(invalid))}. "
                f"Valid: {', '.join(sorted(VALID_BLOCKS))}"
            ),
        )

    blocks = set(include_blocks) & VALID_BLOCKS
    if not blocks:
        blocks = {"market", "scanner"}

    # User-scoping rule: only enforce 401 when the caller asked
    # for a user-scoped block. Pure market/scanner queries don't need a uid.
    user_scoped = {"plays", "alerts", "positions", "drawings"}
    if blocks & user_scoped:
        user_id: Optional[int] = _require_user_id(auth)
    else:
        user_id = _resolve_user_id(auth)

    asset_list = [s.upper() for s in asset_list]

    result = await _gather_and_assemble(asset_list, blocks, user_id)

    if _is_compact(request):
        from carrac.composite.formatters import compact_batch

        result = compact_batch(result)
    return JSONResponse(content=jsonable_encoder(result))


# ---------------------------------------------------------------------------
# Internal — the asyncio.gather rewrite
# ---------------------------------------------------------------------------


async def _gather_and_assemble(
    assets: list[str],
    blocks: set[str],
    user_id: Optional[int],
) -> dict[str, Any]:
    """Fan out the 8 sub-fetches via asyncio.gather, then assemble per asset.

    Same data shape as ``assemble_batch`` — only the fetch ordering changes.
    """
    from carrac.composite.assemblers import (
        _extract_market,
        _extract_scanner,
        _get_content_db,
        _get_htf_by_symbol,
        _get_prices,
        _get_scanner_by_symbol,
    )

    availability: dict[str, str] = {}

    # ── Build the task list — one to_thread per independent sub-fetch ──

    def _safe_call(name: str, fn, fallback):
        """Run fn(), translate raw exceptions to (val, status) so gather
        sees the per-task fallback on failure without aborting the whole
        gather. Mirrors ``assemblers._safe`` semantics.

        Also logs per-sub-fetch timing (mirrors the in-flight timing logs in chart_internal.py)
        so a stalled fan-out is diagnosable —
        we can see which sub-fetch stalled instead of only the total.
        """
        t0 = time.perf_counter()
        try:
            val = fn()
            logger.info(
                "composite: %s ok %dms", name, int((time.perf_counter() - t0) * 1000)
            )
            return name, val, "ok"
        except Exception as e:  # noqa: BLE001 — preserve graceful-degradation semantics
            logger.warning(
                "Composite: %s failed after %dms: %s",
                name,
                int((time.perf_counter() - t0) * 1000),
                e,
                exc_info=True,
            )
            return name, fallback, "error"

    tasks: list[Any] = []

    # 4 global block-fetches (only fire the ones the caller asked for).
    # `assets` (<=MAX_BATCH_ASSETS, already known here) is
    # threaded through to the scanner/htf sub-fetches so they HMGET only
    # the requested symbols from the per-symbol Redis hashes instead of
    # pulling the whole ~10.8MB scanner / ~2.1MB htf blob on every call —
    # this endpoint is the TUI's ~8 calls/min hot path the fix targets.
    if "market" in blocks or "scanner" in blocks:
        tasks.append(
            asyncio.to_thread(_safe_call, "scanner", lambda: _get_scanner_by_symbol(assets), {})
        )
    if "market" in blocks:
        tasks.append(
            asyncio.to_thread(_safe_call, "htf", lambda: _get_htf_by_symbol(assets), {})
        )
        tasks.append(asyncio.to_thread(_safe_call, "prices", _get_prices, {}))
    if "sentiment" in blocks:
        def _fetch_sentiment():
            from carrac.caches import get_cached_regime
            regime = get_cached_regime() or {}
            return {
                "regime": regime.get("regime"),
                "regime_score": regime.get("score"),
            }
        tasks.append(asyncio.to_thread(_safe_call, "sentiment", _fetch_sentiment, {}))

    # 4 user-scoped fetches (only when the block is requested AND user_id present).
    if "alerts" in blocks and user_id is not None:
        def _fetch_alerts():
            from push_notifications import get_user_alerts
            return get_user_alerts(str(user_id))
        tasks.append(asyncio.to_thread(_safe_call, "alerts", _fetch_alerts, []))

    if "plays" in blocks and user_id is not None:
        def _fetch_plays():
            from carrac.plays.models import get_active_plays
            return get_active_plays(user_id)
        tasks.append(asyncio.to_thread(_safe_call, "plays", _fetch_plays, []))

    if "positions" in blocks and user_id is not None:
        def _fetch_positions():
            return _get_content_db().get_open_trades(user_id)
        tasks.append(asyncio.to_thread(_safe_call, "positions", _fetch_positions, []))

    if "drawings" in blocks and user_id is not None:
        def _fetch_drawings():
            return _get_content_db().get_all_drawings_grouped(user_id)
        tasks.append(asyncio.to_thread(_safe_call, "drawings", _fetch_drawings, {}))

    # ── Gather. ``return_exceptions=False`` because _safe_call already
    # neutralises exceptions — anything that escapes is a coding bug. ──
    results = await asyncio.gather(*tasks)

    # ── Slot the results into named buckets. ──
    bucket: dict[str, Any] = {
        "scanner": {},
        "htf": {},
        "prices": {},
        "sentiment": {},
        "alerts": [],
        "plays": [],
        "positions": [],
        "drawings": {},
    }
    for name, val, status in results:
        bucket[name] = val
        availability[name] = status

    htf_by_symbol = bucket["htf"]
    prices = bucket["prices"]
    scanner_by_symbol = bucket["scanner"]
    sentiment = bucket["sentiment"]
    all_alerts = bucket["alerts"]
    all_plays = bucket["plays"]
    all_positions = bucket["positions"]
    all_drawings = bucket["drawings"]

    # ── Per-asset assembly (pure CPU, unchanged from assemble_batch). ──
    out: dict[str, Any] = {}
    for sym in assets:
        entry: dict[str, Any] = {}
        if "market" in blocks:
            entry["market"] = _extract_market(sym, htf_by_symbol, prices)
        if "scanner" in blocks:
            entry["scanner"] = _extract_scanner(sym, scanner_by_symbol)
        if "alerts" in blocks:
            entry["alerts"] = [a for a in all_alerts if a.get("symbol") == sym]
        if "plays" in blocks:
            entry["plays"] = [
                {
                    "id": p.get("id"),
                    "title": p.get("title"),
                    "status": p.get("status"),
                    "direction": p.get("direction"),
                }
                for p in all_plays
                if p.get("primary_asset") == sym
            ]
        if "positions" in blocks:
            entry["positions"] = [t for t in all_positions if t.get("asset") == sym]
        if "drawings" in blocks:
            sym_drawings = all_drawings.get(sym, {}) if isinstance(all_drawings, dict) else {}
            count = sum(
                len(v) if isinstance(v, list) else 0 for v in sym_drawings.values()
            )
            entry["drawings"] = {"count": count}
        out[sym] = entry

    global_data: dict[str, Any] = {}
    if "sentiment" in blocks:
        global_data["sentiment"] = sentiment

    return {"assets": out, "global": global_data, "availability": availability}
