"""Chart-internal endpoint. Cache-aware.

``GET /api/chart-internal/{symbol}``
    OHLCV + indicators for the given symbol. API-key auth only (no session).
    Used by the headless screenshot worker, SDK/MCP consumers, and the
    Morning Analysis Bot (batch-hits ~06:00 UTC).

Auth pattern: ``X-Internal-Key`` or ``X-API-Key`` header **only**.
``?key=`` query param is not accepted — it would leak the secret into
nginx/uvicorn access logs. Pass the key via header instead.

The heavy computation is delegated to
``carrac.charts.service.build_chart_payload`` via
``asyncio.to_thread`` so the sync pandas/Binance work doesn't block the
event loop.

Response cache
----------------------------
Before this, every request unconditionally rebuilt the payload (measured
6.5-9.8s for BTC post-recreate; morning-bot batches median 124.9s/symbol).
Now this router consults ``carrac.caches.get_cached_chart``/
``set_cached_chart`` — the SAME SWR-aware cache the main ``/api/chart``
route uses (Redis, 1h retention, per-TF TTL) — but under a **disjoint key
namespace**. ``/api/chart-internal`` ships a structurally different payload
(``macd`` + ``indicators_ext`` always present, via
``carrac.charts.service.build_chart_payload``) than ``/api/chart`` (built
inline in ``carrac/charts/routes.py``, ``macd``/extended-indicators opt-in).
Sharing a key between the two would poison whichever one reads it second
with the wrong shape — so every key here carries a trailing ``:int``
marker (see ``_cache_tf``) and is never written/read by ``/api/chart``.

Policy: fresh hit -> return cached (``hit``). Stale hit -> return the stale
payload immediately (``stale``) and kick a single-flight, in-process-guarded
background rebuild (chart-internal traffic is low-concurrency, so a simple
per-key in-flight set is enough — no cross-worker Redis lock like
``routes.py``'s SWR mechanism). Absent -> build synchronously
(as before the cache), cache it, log ``miss``. ``?fresh=1`` bypasses the
lookup and forces a synchronous rebuild, still writing the result back
(mirrors ``/api/chart``'s ``?fresh=1``). ``?days=`` alters bar count/content
and isn't part of the cache key, so it bypasses the cache entirely (read
*and* write) rather than fragment the keyspace per value.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from carrac.caches import get_cached_chart, set_cached_chart
from carrac.fastapi_app.limiter import limiter

logger = logging.getLogger("carrac.fastapi.chart_internal")

router = APIRouter()

# ---------------------------------------------------------------------------
# bucket-suffix formula, replicated (NOT imported) from
# carrac/charts/routes.py's ``_FORMING_INTERVAL_MS`` / ``_bucket_ms`` logic.
# Replicated rather than imported because routes.py is a large Flask blueprint
# and this router intentionally has no import-time dependency on it. Keep this dict in sync with routes.py
# if the candle-interval table there ever changes.
# ---------------------------------------------------------------------------
_FORMING_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
}

# In-process single-flight guard for background SWR rebuilds: at most one
# rebuild in flight per (symbol, cache_tf) at a time. Chart-internal traffic
# is low-concurrency (screenshot worker / SDK / MCP / morning bot), so an
# in-process set is enough — worst case across N gunicorn/uvicorn workers is
# N redundant rebuilds (one per process), not an unbounded stampede.
_inflight: set[str] = set()
_inflight_lock = threading.Lock()

# Hold strong references to fire-and-forget background tasks — asyncio only
# keeps a weak reference to a Task once the creator drops its own, so an
# unreferenced task can be garbage-collected mid-flight.
_background_tasks: "set[asyncio.Task]" = set()


def _acquire_inflight(key: str) -> bool:
    with _inflight_lock:
        if key in _inflight:
            return False
        _inflight.add(key)
        return True


def _release_inflight(key: str) -> None:
    with _inflight_lock:
        _inflight.discard(key)


def _cache_tf(tf: str) -> str:
    """Distinct chart-internal cache-key TF variant.

    The ``:int`` marker keeps this namespace disjoint from ``/api/chart``'s
    plain ``{tf}`` / ``{tf}:col`` / ``{tf}:q...`` keys (different payload
    shape — see module docstring). The trailing ``:b<bucket>`` suffix —
    appended LAST, mirroring routes.py — keeps an intraday payload from
    being served past its candle close; ``caches.py``'s bucket-stripped-alias
    logic only strips a trailing ``:b\\d+``.
    """
    cache_tf = f"{tf}:int"
    bucket_ms = _FORMING_INTERVAL_MS.get(str(tf).lower())
    if bucket_ms:
        cache_tf += f":b{int(time.time() * 1000) // bucket_ms}"
    return cache_tf


def _build_and_cache(
    symbol: str,
    mode: str,
    timeframe: Optional[str],
    days: Optional[int],
    cache_tf: str,
) -> dict:
    """Synchronous build + cache write. Always runs off the event loop
    (foreground miss/fresh=1 via ``asyncio.to_thread``, or a background SWR
    rebuild via the same)."""
    from carrac.charts.service import build_chart_payload

    payload = build_chart_payload(symbol, mode=mode, timeframe=timeframe, days=days)
    set_cached_chart(symbol.upper(), cache_tf, False, payload)
    return payload


def _spawn_background_rebuild(
    symbol: str,
    mode: str,
    timeframe: Optional[str],
    days: Optional[int],
    cache_tf: str,
) -> None:
    """Fire a bounded, single-flight background rebuild for a stale entry.

    No-op if a rebuild for this exact key is already in flight.
    """
    inflight_key = f"{symbol.upper()}:{cache_tf}"
    if not _acquire_inflight(inflight_key):
        return

    async def _run() -> None:
        try:
            await asyncio.to_thread(_build_and_cache, symbol, mode, timeframe, days, cache_tf)
        except Exception as exc:  # best-effort — the stale caller already got a response
            logger.warning("chart-internal background refresh failed for %s: %s", inflight_key, exc)
        finally:
            _release_inflight(inflight_key)

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _verify_key(request: Request) -> tuple[bool, bool]:
    """Check X-Internal-Key / X-API-Key header only.

    Returns (ok, key_in_query_param) where the second element signals that
    the caller incorrectly sent the key as a ?key= URL query param (which
    must be rejected with 400 to prevent secret exposure in access logs).

    Delegates to ``carrac.auth.decorators.resolve_api_key`` which is the
    runtime-agnostic twin of ``verify_api_key`` (doesn't touch flask.g).
    """
    # Reject ?key= immediately — it leaks the secret into nginx/uvicorn logs.
    if request.query_params.get("key"):
        return False, True

    # The shared key resolves only when it arrived on
    # X-Internal-Key — hence allow_shared keyed to which header had it.
    internal = request.headers.get("X-Internal-Key", "")
    key = internal or request.headers.get("X-API-Key", "")
    if not key:
        return False, False
    from carrac.auth.decorators import resolve_api_key

    ok, _uid, _tier, _scope, _internal = resolve_api_key(
        key, allow_shared=bool(internal))
    return ok, False


@router.get("/api/chart-internal/{symbol}")
@limiter.limit("120/minute")
async def get_chart_data_internal(
    request: Request,
    symbol: str,
    mode: Optional[str] = "htf",
    timeframe: Optional[str] = None,
    days: Optional[int] = None,
    fresh: Optional[str] = None,
) -> JSONResponse:
    """OHLCV + indicators for ``symbol``. API-key auth only.

    Query params
    ------------
    mode : ``'htf'`` (default) | ``'ltf'`` — only consulted when
        ``timeframe`` is absent (mirrors ``build_chart_payload``).
    timeframe : ``'1D'`` | ``'4h'`` | ``'5m'`` | …
    days : max bars to return. Alters payload content and is NOT part of
        the cache key — a request with ``days`` set bypasses the cache
        entirely (read and write) rather than fragment the keyspace.
    fresh : ``'1'`` bypasses the cache lookup and forces a synchronous
        rebuild, still writing the fresh result back (mirrors
        ``/api/chart``'s ``?fresh=1``). Use when a caller needs
        guaranteed-current data instead of a possibly-stale SWR hit.

    Auth
    ----
    Pass the API key via ``X-API-Key`` or ``X-Internal-Key`` header.
    ``?key=`` URL param is rejected (400) — it leaks secrets into access logs.
    """
    ok, key_in_qp = await asyncio.to_thread(_verify_key, request)
    if key_in_qp:
        return JSONResponse(
            {"error": "API key via URL parameter is not allowed. Use X-API-Key header."},
            status_code=400,
        )
    if not ok:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    from carrac.charts.service import ChartNotFoundError, build_chart_payload

    t0 = time.perf_counter()
    mode_resolved = mode or "htf"
    tf = timeframe or ("4h" if mode_resolved == "ltf" else "1D")
    # Canonicalize case ('4H'→'4h', '1d'→'1D'; 1m vs 1M untouched) BEFORE the
    # cache key is derived — the morning engine requests '4H', which missed
    # every chartwarm-warmed '4h' key and paid 30-60s cold builds each run
    # (build_chart_payload normalizes internally, so only the key/log differed).
    from carrac.charts.routes import _TF_CANON
    tf = _TF_CANON.get(tf, tf)
    symbol_upper = symbol.upper()
    cache_tf = _cache_tf(tf)

    fresh_flag = (fresh or "0") == "1"
    has_days = days is not None
    # fresh=1 bypasses the READ but still writes back (mirrors /api/chart).
    # days bypasses BOTH — it isn't part of the key, so writing it back
    # under the base key would poison the next unparametrised request.
    skip_read = fresh_flag or has_days
    skip_write = has_days

    try:
        if not skip_read:
            cached = await asyncio.to_thread(get_cached_chart, symbol_upper, cache_tf, False)
            if cached is not None:
                cached_payload, is_fresh = cached
                if is_fresh:
                    logger.info(
                        "chart %s tf=%s hit %dms", symbol_upper, tf,
                        int((time.perf_counter() - t0) * 1000),
                    )
                    return JSONResponse(cached_payload)
                age_s = max(0, int(time.time() - (cached_payload.get("_cached_at") or time.time())))
                logger.info(
                    "chart %s tf=%s stale %dms age=%ds", symbol_upper, tf,
                    int((time.perf_counter() - t0) * 1000), age_s,
                )
                _spawn_background_rebuild(symbol, mode_resolved, tf, days, cache_tf)
                return JSONResponse(cached_payload)

        if skip_write:
            payload = await asyncio.to_thread(
                build_chart_payload,
                symbol,
                mode=mode_resolved,
                timeframe=tf,
                days=days,
            )
        else:
            payload = await asyncio.to_thread(
                _build_and_cache, symbol, mode_resolved, tf, days, cache_tf,
            )
        logger.info("chart %s tf=%s miss %dms", symbol_upper, tf, int((time.perf_counter() - t0) * 1000))
        return JSONResponse(payload)
    except ChartNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        logger.error(
            "Chart internal API error for %s: %s", symbol, exc, exc_info=True
        )
        return JSONResponse({"error": "Internal chart data error"}, status_code=500)
