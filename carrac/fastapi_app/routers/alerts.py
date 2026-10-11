"""Alerts endpoints.

Migrates the following Flask ``alerts_bp`` handlers to FastAPI:

  POST  /api/alerts                  — price alert
  POST  /api/alerts/trendline
  POST  /api/alerts/indicator
  POST  /api/alerts/formula
  POST  /api/alerts/zscore           — non-free tier gate
  POST  /api/alerts/regime           — non-free tier gate
  POST  /api/alerts/chain_strength   — non-free tier gate
  POST  /api/alerts/anomaly_cluster  — non-free tier gate
  POST  /api/alerts/conditional      — non-free tier gate
  PUT   /api/alerts/{alert_id}
  DELETE /api/alerts/{alert_id}
  POST  /api/notification-settings
  POST  /api/user/analyst-sources

CSRF policy: ``alerts_bp`` is NOT csrf.exempt in Flask (global flask-wtf
covers it, plus ``_enforce_alerts_csrf`` before_request adds a manual
gate). ``/api/alerts`` is NOT in ``_SKIP_PREFIXES`` in
``CarracCSRFMiddleware``, so CSRF is enforced by the middleware for
session callers. X-API-Key callers bypass CSRF (matches Flask behaviour —
see the middleware's ``X-API-Key present`` branch). No change to the
middleware is needed.

Validation strategy: Pydantic body models enforce shape and
``extra="forbid"``. Deep semantic validation (SSRF, indicator whitelist,
formula parse, CANONICAL_METRICS) stays in Flask helpers — the FastAPI
handlers call them (or replicate their logic inline for helpers that
aren't importable without a Flask app context).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    auth_or_key,
    get_request_tier,
)
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.alerts import (
    AlertCreateRequest,
    AlertUpdateRequest,
    AnalystSourcesRequest,
    AnomalyClusterAlertRequest,
    ChainStrengthAlertRequest,
    ConditionalAlertRequest,
    FormulaAlertRequest,
    IndicatorAlertRequest,
    NotificationSettingsRequest,
    RegimeAlertRequest,
    TrendlineAlertRequest,
    ZscoreAlertRequest,
)

router = APIRouter()


def _resolve_user_id(auth: dict) -> Optional[str]:
    """Return the string user_id matching ``alerts_bp._resolve_alert_user_id``.

    Alerts use *string* user_ids (``str(uid)``).  Per-user API key → the
    bound int cast to str; session → session user_id cast to str; shared
    admin key (user_id=None) → None (caller emits 401).
    """
    if auth.get("source") == "api_key":
        uid = auth.get("user_id")
        return str(uid) if uid is not None else None
    user = auth.get("user") or {}
    uid = user.get("id")
    return str(uid) if uid is not None else None


def _int_user_id(auth: dict) -> Optional[int]:
    """Return integer user_id for handlers that use ``int(session['user_id'])``."""
    if auth.get("source") == "api_key":
        return auth.get("user_id")  # already int or None
    user = auth.get("user") or {}
    uid = user.get("id")
    return int(uid) if uid is not None else None


def _alert_limit_gate(user_id: str, request: Request) -> None:
    """Enforce the per-tier alert count limit.

    Replicates the common guard block used in six Flask alert handlers.
    Raises ``HTTPException(403)`` when the limit is reached. No-op for
    ``user_id == 'admin'`` (matches Flask behaviour).
    """
    if user_id == "admin":
        return
    import push_notifications
    from carrac.auth.decorators import get_tier_limits

    tier = get_request_tier(request)
    limits = get_tier_limits(tier)
    current_alerts = push_notifications.get_user_alerts(user_id, active_only=True)
    if len(current_alerts) >= limits["max_alerts"]:
        raise HTTPException(
            status_code=403,
            detail={
                "error": (
                    f"Alert limit reached ({len(current_alerts)}/{limits['max_alerts']})."
                    " Upgrade for more."
                ),
                "limit_type": "alert",
                "upgrade_required": True,
                "current": len(current_alerts),
                "max": limits["max_alerts"],
            },
        )


def _channel_guard(user_id: str) -> None:
    """Require at least one notification channel. 400 if none configured."""
    import push_notifications

    if not push_notifications.user_has_notification_channel(user_id):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Configure at least one notification channel before creating alerts.",
                "no_channel": True,
            },
        )


def _qe_alert_guard(user_id: str, request: Request) -> None:
    """QE alert guard — non-free tier + channel + limit check.

    Replicates ``alerts_bp._qe_alert_guard``. Raises ``HTTPException`` on
    any of the three failure modes (free tier, no channel, limit exceeded).
    """
    tier = get_request_tier(request)
    if tier == "free":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Quant Engine alerts require a paid subscription",
                "upgrade_required": True,
            },
        )
    _channel_guard(user_id)
    _alert_limit_gate(user_id, request)


# ---------------------------------------------------------------------------
# POST /api/alerts — price alert
# ---------------------------------------------------------------------------


@router.post("/api/alerts", status_code=201)
@limiter.limit("30/minute")
async def create_alert(
    request: Request,
    body: AlertCreateRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a price alert (above/below threshold)."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Per-user API key required for alert operations",
        )

    import push_notifications

    _channel_guard(user_id)
    _alert_limit_gate(user_id, request)

    alert = await asyncio.to_thread(
        push_notifications.create_alert,
        user_id=user_id,
        symbol=body.symbol,
        condition=body.condition,
        price=body.price,
        note=body.note,
        frequency=body.frequency,
        journal_id=body.journal_id,
        auto_close=body.auto_close,
    )

    # first_alert_created funnel event.
    if user_id != "admin":
        try:
            from carrac.integrations import fire_first_event

            fire_first_event(user_id, "first_alert_created", {"symbol": body.symbol})
        except Exception:
            pass

    return JSONResponse({"alert": alert}, status_code=201)


# ---------------------------------------------------------------------------
# POST /api/alerts/trendline
# ---------------------------------------------------------------------------


@router.post("/api/alerts/trendline", status_code=201)
@limiter.limit("30/minute")
async def create_trendline_alert(
    request: Request,
    body: TrendlineAlertRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a trendline break alert."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Per-user API key required for alert operations",
        )

    import push_notifications

    _channel_guard(user_id)
    _alert_limit_gate(user_id, request)

    alert = await asyncio.to_thread(
        push_notifications.create_trendline_alert,
        user_id=user_id,
        symbol=body.symbol,
        drawing_id=body.drawing_id,
        p1_ts=body.p1_ts,
        p1_val=body.p1_val,
        p2_ts=body.p2_ts,
        p2_val=body.p2_val,
        note=body.note,
        frequency=body.frequency,
    )
    return JSONResponse({"alert": alert}, status_code=201)


# ---------------------------------------------------------------------------
# POST /api/alerts/indicator
# ---------------------------------------------------------------------------


@router.post("/api/alerts/indicator", status_code=201)
@limiter.limit("30/minute")
async def create_indicator_alert(
    request: Request,
    body: IndicatorAlertRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create an indicator state-change alert."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Per-user API key required for alert operations",
        )

    import push_notifications

    _channel_guard(user_id)
    _alert_limit_gate(user_id, request)

    # Validate indicator against INDICATOR_ALERT_CONDITIONS (server-side).
    from carrac.indicator_state import INDICATOR_ALERT_CONDITIONS

    if body.indicator not in INDICATOR_ALERT_CONDITIONS:
        raise HTTPException(
            status_code=400,
            detail=f'Invalid indicator. Valid: {", ".join(INDICATOR_ALERT_CONDITIONS.keys())}',
        )
    valid_states = INDICATOR_ALERT_CONDITIONS.get(body.indicator)
    if valid_states is not None and body.target_state not in valid_states:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid target_state for {body.indicator}. Valid: {valid_states}",
        )

    alert = await asyncio.to_thread(push_notifications.insert_alert, {
        "user_id": user_id,
        "symbol": body.symbol.upper(),
        "alert_type": "indicator",
        "indicator": body.indicator,
        "target_state": body.target_state,
        "note": body.note,
        "frequency": body.frequency,
        "active": True,
        "triggered": False,
    })

    return JSONResponse({"alert": alert}, status_code=201)


# ---------------------------------------------------------------------------
# POST /api/alerts/formula
# ---------------------------------------------------------------------------


@router.post("/api/alerts/formula", status_code=201)
@limiter.limit("30/minute")
async def create_formula_alert(
    request: Request,
    body: FormulaAlertRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a formula-based alert."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Per-user API key required for alert operations",
        )

    import push_notifications
    from carrac.formula_eval import validate_formula

    _channel_guard(user_id)
    _alert_limit_gate(user_id, request)

    # Deep formula validation (server-side — Pydantic can't replicate AST parse).
    validation = validate_formula(body.expression)
    if not validation.get("valid"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid formula: {validation.get('error', 'parse error')}",
        )

    alert = await asyncio.to_thread(push_notifications.insert_alert, {
        "user_id": user_id,
        "symbol": body.symbol.upper(),
        "alert_type": "formula",
        "expression": body.expression,
        "timeframe": body.timeframe,
        "note": body.note,
        "frequency": body.frequency,
        "active": True,
        "triggered": False,
    })

    return JSONResponse({"alert": alert}, status_code=201)


# ---------------------------------------------------------------------------
# POST /api/alerts/zscore — non-free tier gate
# ---------------------------------------------------------------------------


@router.post("/api/alerts/zscore", status_code=201)
@limiter.limit("30/minute")
async def create_zscore_alert(
    request: Request,
    body: ZscoreAlertRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a z-score threshold alert (QE nightly pipeline)."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    import push_notifications

    _qe_alert_guard(user_id, request)

    metric = body.metric.strip().upper()

    # Validate metric against CANONICAL_METRICS when QE is available.
    try:
        from quant.engine.config import CANONICAL_METRICS

        if metric not in CANONICAL_METRICS:
            raise HTTPException(status_code=400, detail=f"Unknown metric: {metric}")
    except ImportError:
        pass  # QE not installed — skip validation

    alert = await asyncio.to_thread(
        push_notifications.create_zscore_alert,
        user_id=user_id,
        metric=metric,
        condition=body.condition,
        threshold=body.threshold,
        window=body.window,
        note=body.note,
        frequency=body.frequency,
    )
    return JSONResponse({"alert": alert}, status_code=201)


# ---------------------------------------------------------------------------
# POST /api/alerts/regime — non-free tier gate
# ---------------------------------------------------------------------------


@router.post("/api/alerts/regime", status_code=201)
@limiter.limit("30/minute")
async def create_regime_alert(
    request: Request,
    body: RegimeAlertRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a regime transition alert (QE nightly pipeline)."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    import push_notifications

    _qe_alert_guard(user_id, request)

    alert = await asyncio.to_thread(
        push_notifications.create_regime_alert,
        user_id=user_id,
        note=body.note,
        frequency=body.frequency,
    )
    return JSONResponse({"alert": alert}, status_code=201)


# ---------------------------------------------------------------------------
# POST /api/alerts/chain_strength — non-free tier gate
# ---------------------------------------------------------------------------


@router.post("/api/alerts/chain_strength", status_code=201)
@limiter.limit("30/minute")
async def create_chain_strength_alert(
    request: Request,
    body: ChainStrengthAlertRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a transmission chain-strength alert."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    import push_notifications

    _qe_alert_guard(user_id, request)

    try:
        alert = await asyncio.to_thread(
            push_notifications.create_chain_strength_alert,
            user_id=user_id,
            chain=body.chain,
            condition=body.condition,
            threshold=body.threshold,
            frequency=body.frequency,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"alert": alert}, status_code=201)


# ---------------------------------------------------------------------------
# POST /api/alerts/anomaly_cluster — non-free tier gate
# ---------------------------------------------------------------------------


@router.post("/api/alerts/anomaly_cluster", status_code=201)
@limiter.limit("30/minute")
async def create_anomaly_cluster_alert(
    request: Request,
    body: AnomalyClusterAlertRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create an anomaly-cluster alert."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    import push_notifications

    _qe_alert_guard(user_id, request)

    try:
        alert = await asyncio.to_thread(
            push_notifications.create_anomaly_cluster_alert,
            user_id=user_id,
            min_domains=body.min_domains,
            min_metrics=body.min_metrics,
            frequency=body.frequency,
            note=body.note,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"alert": alert}, status_code=201)


# ---------------------------------------------------------------------------
# POST /api/alerts/conditional — non-free tier gate
# ---------------------------------------------------------------------------


@router.post("/api/alerts/conditional", status_code=201)
@limiter.limit("30/minute")
async def create_conditional_alert(
    request: Request,
    body: ConditionalAlertRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a conditional z-score alert."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    import push_notifications

    _qe_alert_guard(user_id, request)

    # body.conditions is already normalised to list[str] by the model_validator.
    conditions = body.conditions if isinstance(body.conditions, list) else []

    try:
        alert = await asyncio.to_thread(
            push_notifications.create_conditional_alert,
            user_id=user_id,
            conditions=conditions,
            target=body.target,
            horizon=body.horizon,
            frequency=body.frequency,
            note=body.note,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse({"alert": alert}, status_code=201)


# ---------------------------------------------------------------------------
# PUT /api/alerts/{alert_id}
# ---------------------------------------------------------------------------


@router.put("/api/alerts/{alert_id}")
@limiter.limit("30/minute")
async def update_alert(
    request: Request,
    body: AlertUpdateRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    alert_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Update editable fields on a price alert."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    import push_notifications

    data = body.model_dump(exclude_unset=True)
    alert = await asyncio.to_thread(push_notifications.update_alert, user_id, alert_id, data)
    if alert:
        return JSONResponse({"alert": alert})
    raise HTTPException(status_code=404, detail="Alert not found")


# ---------------------------------------------------------------------------
# DELETE /api/alerts/{alert_id}
# ---------------------------------------------------------------------------


@router.delete("/api/alerts/{alert_id}")
@limiter.limit("30/minute")
async def delete_alert(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    alert_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Delete a price alert."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    import push_notifications

    success = await asyncio.to_thread(push_notifications.delete_alert, user_id, alert_id)
    if success:
        return JSONResponse({"status": "deleted"})
    raise HTTPException(status_code=404, detail="Alert not found")


# ---------------------------------------------------------------------------
# POST /api/notification-settings
# ---------------------------------------------------------------------------


@router.post("/api/notification-settings")
@limiter.limit("30/minute")
async def save_notification_settings(
    request: Request,
    body: NotificationSettingsRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Save notification preferences for the current user."""
    uid = _int_user_id(auth)
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.auth.decorators import get_tier_limits

    tier = get_request_tier(request)
    limits = get_tier_limits(tier)

    # One implementation for both doors. A hand-copied subset of the Flask
    # route drifts (no webhook tier gate, no token check, and a wholesale
    # prefs overwrite that drops the webhook secret), so delegate.
    from carrac.alerts.routes import apply_notification_settings

    payload, status = await asyncio.to_thread(
        apply_notification_settings, uid, body.model_dump(), limits
    )
    return JSONResponse(payload, status_code=status)


# ---------------------------------------------------------------------------
# POST /api/user/analyst-sources
# ---------------------------------------------------------------------------


@router.post("/api/user/analyst-sources")
@limiter.limit("30/minute")
async def save_analyst_sources(
    request: Request,
    body: AnalystSourcesRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Save user's followed X handles and YouTube channels."""
    uid = _int_user_id(auth)
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    import re

    _MAX_X_HANDLES = 10
    _MAX_YT_CHANNELS = 20

    if len(body.x_handles) > _MAX_X_HANDLES:
        raise HTTPException(
            status_code=400, detail=f"Maximum {_MAX_X_HANDLES} X handles"
        )
    if len(body.yt_channels) > _MAX_YT_CHANNELS:
        raise HTTPException(
            status_code=400, detail=f"Maximum {_MAX_YT_CHANNELS} YouTube channels"
        )

    # Clean X handles — strip @, reject invalid, silently drop bad entries.
    cleaned_handles = []
    for h in body.x_handles:
        h = str(h).strip().lstrip("@")
        if h and re.match(r"^[A-Za-z0-9_]{1,30}$", h):
            cleaned_handles.append(h)

    # Validate YT channel IDs (must start with UC + exactly 24 chars).
    cleaned_yt = {}
    for cid, name in body.yt_channels.items():
        cid = str(cid).strip()
        name = str(name).strip()[:50]
        if cid.startswith("UC") and len(cid) == 24 and name:
            cleaned_yt[cid] = name

    from user_db import user_db

    def _save_analyst_sources():
        prefs = user_db.get_preferences(uid)
        prefs["followed_x_handles"] = cleaned_handles
        prefs["followed_yt_channels"] = cleaned_yt
        user_db.save_preferences(uid, prefs)

    await asyncio.to_thread(_save_analyst_sources)

    return JSONResponse(
        {
            "status": "saved",
            "x_handles": cleaned_handles,
            "yt_channels": cleaned_yt,
        }
    )
