"""Preferences write endpoints.

Three endpoints:

* ``POST /api/indicator-prefs``    — merge indicator preferences
* ``POST /api/preferences/sync``   — sync one preference key (tier-gated)
* ``POST /api/settings/save``      — save one preference key (no tier gate)

CSRF: ``/api/preferences`` and
``/api/settings`` are listed in the middleware's ``_SKIP_PREFIXES``, so
``CarracCSRFMiddleware`` does NOT enforce CSRF on these paths.

Per-key semantic validation (report_indicators filtering, report_assets
shape, column_prefs whitelist, report_schedule region) is inline in each
handler.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    auth_or_key,
    get_request_tier,
)
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.preferences import (
    IndicatorPrefsRequest,
    PreferencesSyncRequest,
    SettingsSaveRequest,
)

logger = logging.getLogger("carrac.fastapi.preferences")

router = APIRouter()

# Whitelist of syncable preference keys.
_ALLOWED_PREF_KEYS: frozenset[str] = frozenset(
    {
        "display_options",
        "chart_prefs",
        "favourite_assets",
        "watchlist_order",
        "alert_prefs",
        "scenario_panel",
        "morning_report_prefs",
        "report_indicators",
        "report_assets",
        "column_prefs",
        "ticker_prefs",
        "news_panel_prefs",
        "tf_favourites",
        "report_schedule",
    }
)

_VALID_REPORT_INDICATORS: frozenset[str] = frozenset(
    {
        "RSI", "RSI_DIV", "MACD", "TTM_SQUEEZE", "BBW_PCT", "ATR_PCT",
        "NR7", "WILLIAMS_R", "CCI", "STOCH", "STOCH_RSI",
        "MA", "EMA", "DAILY_EMA", "WEEKLY_MA", "BULL_BAND", "CM_ULTIMATE",
        "EMA_RIBBON", "CONSENSIO", "CONSENSIO_LITE", "ALLIGATOR", "ICHIMOKU",
        "GAUSS_CHAN", "PI_CYCLE", "SMA_SIGNAL", "BOLL", "KELTNER", "KELTNER_TIGHT",
        "DONCHIAN", "PSAR", "HULL_MA", "EMA_CROSS", "SUPER_TREND", "ALMA", "KAMA",
        "CPR", "CPR_HISTORY", "KEY_LEVELS", "MON_RANGE", "SR_CHANNELS",
        "AUTO_FIB", "FVG",
        "VOL", "VP", "OBV", "CVD_SPOT", "CVD_DIV", "CVD_USDT", "CVD_COIN",
        "OI", "DERIV_COMBINED",
    }
)

_VALID_ASSET_TFS: frozenset[str] = frozenset({"4H", "1D", "1W"})

_VALID_COLUMNS: frozenset[str] = frozenset(
    {
        "name", "wrsi", "cpr", "bb", "trend4h", "macd", "adx", "atr", "vol",
        "signals", "ichimoku", "supertrend", "psar", "emacross", "alligator",
        "stochrsi", "ttmsqueeze", "donchian", "nr7", "bbwpct", "atrpctrank",
        "obv", "fvg", "vplevels", "rsidiv",
    }
)

_VALID_REGIONS: frozenset[str] = frozenset(
    {"europe", "us_east", "us_west", "asia_pacific", "asia"}
)


def _require_user_id(auth: dict) -> int:
    """Resolve user_id; 401 if absent."""
    if auth.get("source") == "api_key":
        uid = auth.get("user_id")
    else:
        user = auth.get("user") or {}
        uid = user.get("id")
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return uid


def _resolve_user_id_or_admin(auth: dict, request: Request) -> int:
    """Resolve user_id for indicator-prefs: session OR shared admin API key.

    The shared admin key resolves through
    ``verify_api_key(...) → admin_email → user_db.get_user_by_email(...)``.
    """
    if auth.get("source") == "api_key":
        uid = auth.get("user_id")
        if uid is not None:
            return uid
        # Shared admin key — resolve via admin email env var
        from user_db import user_db

        admin_email = os.getenv("ADMIN_EMAIL", "")
        if admin_email:
            admin = user_db.get_user_by_email(admin_email)
            if admin:
                return admin["id"]
        raise HTTPException(status_code=500, detail="Admin user not found")
    user = auth.get("user") or {}
    uid = user.get("id")
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return uid


def _validate_and_clean_pref_value(
    pref_key: str, value: Any, tier: str
) -> Any:
    """Inline per-key validation shared by the sync and save handlers."""
    if pref_key == "report_indicators":
        if not isinstance(value, list):
            raise HTTPException(
                status_code=400, detail="report_indicators must be a list"
            )
        return [v for v in value if isinstance(v, str) and v in _VALID_REPORT_INDICATORS]

    if pref_key == "report_assets":
        if not isinstance(value, list):
            raise HTTPException(
                status_code=400, detail="report_assets must be a list"
            )
        max_items = 30 if tier in ("premium", "admin") else 15
        cleaned = []
        for item in value[:max_items]:
            if not isinstance(item, dict):
                continue
            asset = str(item.get("asset", "")).upper().strip()
            tfs = item.get("timeframes", [])
            if not asset or not isinstance(tfs, list):
                continue
            tfs = [t for t in tfs if isinstance(t, str) and t in _VALID_ASSET_TFS]
            if tfs:
                cleaned.append({"asset": asset, "timeframes": tfs})
        return cleaned

    if pref_key == "column_prefs":
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=400, detail="column_prefs must be a dict"
            )
        visible = value.get("visible", [])
        if isinstance(visible, list):
            value = dict(value)
            value["visible"] = [v for v in visible if v in _VALID_COLUMNS]
        return value

    if pref_key == "report_schedule":
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=400, detail="report_schedule must be a dict"
            )
        region = str(value.get("region", "europe")).strip().lower()
        if region not in _VALID_REGIONS:
            region = "europe"
        return {"region": region}

    # All other keys: pass through as-is.
    return value


# ---------------------------------------------------------------------------
# POST /api/indicator-prefs
# ---------------------------------------------------------------------------


@router.post("/api/indicator-prefs")
@limiter.limit("60/minute")
async def indicator_prefs(
    request: Request,
    body: IndicatorPrefsRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Merge indicator preferences.

    Merge semantics: each present field is merged into the stored
    ``indicator_prefs`` sub-dict — NOT a full replace.

    Auth: session OR admin API key (not per-user key).
    """
    # _resolve_user_id_or_admin may hit user_db.get_user_by_email on the
    # shared-admin-key path — offload the whole resolver call.
    user_id = await asyncio.to_thread(_resolve_user_id_or_admin, auth, request)

    from carrac.charts.routes import _migrate_indicator_prefs
    from carrac.sync.events import publish_preference_event
    from user_db import user_db

    def _do_indicator_prefs():
        all_prefs = user_db.get_preferences(user_id)
        ind_prefs = all_prefs.get("indicator_prefs", {})

        # Merge — only update keys that were sent.
        if body.indicators is not None:
            ind_prefs["indicators"] = body.indicators
        if body.favourites is not None:
            ind_prefs["favourites"] = body.favourites
        if body.groups is not None:
            ind_prefs["groups"] = body.groups
        if body.params is not None:
            ind_prefs["params"] = body.params

        _migrate_indicator_prefs(ind_prefs)

        all_prefs["indicator_prefs"] = ind_prefs
        user_db.save_preferences(user_id, all_prefs)

    await asyncio.to_thread(_do_indicator_prefs)

    # Tiny Redis-only micro-call — not worth thread-offloading.
    sync_sid = request.headers.get("X-Sync-Session", "")
    publish_preference_event(user_id, sync_sid)

    return JSONResponse({"success": True})


# ---------------------------------------------------------------------------
# POST /api/preferences/sync
# ---------------------------------------------------------------------------


@router.post("/api/preferences/sync")
@limiter.limit("60/minute")
async def preferences_sync(
    request: Request,
    body: PreferencesSyncRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Upsert one user preference key.

    Tier-gated: ``cross_device_sync`` required.
    """
    user_id = _require_user_id(auth)

    from carrac.auth.decorators import get_tier_limits
    from carrac.sync.events import publish_preference_event
    from user_db import user_db

    # Tier gate — cross-device sync requires a paid subscription.
    tier = get_request_tier(request)
    if not get_tier_limits(tier).get("cross_device_sync"):
        return JSONResponse(
            {"error": "Cross-device sync requires a subscription", "upgrade_required": True},
            status_code=403,
        )

    pref_key = body.key
    if pref_key not in _ALLOWED_PREF_KEYS:
        raise HTTPException(
            status_code=400, detail=f"Unknown preference key: {pref_key}"
        )

    value = _validate_and_clean_pref_value(pref_key, body.value, tier)

    def _do_prefs_sync():
        all_prefs = user_db.get_preferences(user_id)
        all_prefs[pref_key] = value
        user_db.save_preferences(user_id, all_prefs)

    await asyncio.to_thread(_do_prefs_sync)

    # Tiny Redis-only micro-call — not worth thread-offloading.
    sync_sid = request.headers.get("X-Sync-Session", "")
    publish_preference_event(user_id, sync_sid)

    return JSONResponse({"success": True})


# ---------------------------------------------------------------------------
# POST /api/settings/save
# ---------------------------------------------------------------------------


@router.post("/api/settings/save")
@limiter.limit("30/minute")
async def settings_save(
    request: Request,
    body: SettingsSaveRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Save one user preference key — NO tier gate.

    For paid tiers, also publishes a sync event (so cross-device sync
    picks up the change).
    """
    user_id = _require_user_id(auth)

    from carrac.auth.decorators import get_tier_limits
    from user_db import user_db

    pref_key = body.key
    if pref_key not in _ALLOWED_PREF_KEYS:
        raise HTTPException(
            status_code=400, detail=f"Unknown preference key: {pref_key}"
        )

    tier = get_request_tier(request)
    value = _validate_and_clean_pref_value(pref_key, body.value, tier)

    def _do_settings_save():
        all_prefs = user_db.get_preferences(user_id)
        all_prefs[pref_key] = value
        user_db.save_preferences(user_id, all_prefs)

    await asyncio.to_thread(_do_settings_save)

    # Publish sync event for paid tiers — best-effort, never breaks request.
    if get_tier_limits(tier).get("cross_device_sync"):
        try:
            from carrac.sync.events import publish_preference_event

            sync_sid = request.headers.get("X-Sync-Session", "")
            publish_preference_event(user_id, sync_sid)
        except Exception:
            pass

    return JSONResponse({"success": True})
