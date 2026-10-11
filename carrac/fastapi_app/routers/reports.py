"""Morning report read endpoints — FastAPI port.

* ``GET /api/report/morning/latest`` — latest morning report JSON.

The handler splices a per-user personal section into the shared report.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    auth_or_key,
    get_request_tier,
)
from carrac.fastapi_app.limiter import limiter

logger = logging.getLogger("carrac.fastapi.reports")

router = APIRouter()

# How old the newest 'morning' row may be before this route stops
# calling it "the latest report". Matches _LATEST_AUDIO_MAX_AGE_DAYS in
# carrac/reports/routes.py — the audio and the text for the same report must
# not disagree about what "current" means.
_MORNING_MAX_AGE_DAYS = 2.0


def _stale_morning_alarm(context: str) -> None:
    """Human-reaching alarm when a 'morning' row exists but is too old to serve.

    Silence is the bug in this class: the route degrades
    correctly but nothing says the pipeline stopped writing. Same shape as
    carrac/reports/prior_day.py::_alarm.
    """
    msg = (
        f"Morning report is stale (older than {_MORNING_MAX_AGE_DAYS} days) and was "
        f"withheld from {context}. The route fell back rather than serving old "
        f"content, so no user saw a stale report — but the morning pipeline has "
        f"stopped producing. Check ENABLE_MORNING_REPORT and the report engine "
        f"(/var/log/morning-engine.log on the app host)."
    )
    logger.error("STALE-MORNING ALARM: %s", msg)
    try:
        import sentry_sdk

        sentry_sdk.capture_message(msg, level="error")
    except Exception:  # noqa: BLE001
        # logger.error above already fired — this is not a silent loss.
        logger.warning("stale-morning alarm: Sentry capture failed", exc_info=True)


def _public_report_metadata(meta: dict) -> dict:
    """The part of a report's metadata any logged-in reader may see.

    Only each data source's ok/error status survives: the ``detail`` text
    can carry upstream error messages and internal URLs.
    """
    sources = meta.get("data_sources")
    if not isinstance(sources, dict):
        return {}
    return {
        "data_sources": {
            name: {"status": src.get("status")}
            for name, src in sources.items()
            if isinstance(src, dict)
        }
    }


@router.get("/api/report/morning/latest")
@limiter.limit("24/minute")
async def get_latest_morning_report(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Latest morning report; splices in the caller's personal section if eligible."""
    # Tier gate. The operator's own morning report (positions/alerts woven in)
    # is ADMIN-ONLY: every other tier — paid included — is served the
    # operator-neutral shared "general" report (position-tracking reports
    # return as a per-user paid feature, not a window into the operator's
    # book). NOTE: this is the LIVE handler for /api/report/morning/latest
    # (strangler MIGRATED_PATHS).
    sess = getattr(request.state, "session", {}) or {}
    user_id = sess.get("user_id")
    tier = get_request_tier(request)
    try:
        from carrac.auth.decorators import get_tier_limits

        limits = get_tier_limits(tier)
    except Exception:
        limits = {}
    # ?as=general previews the shared operator-neutral "general" report exactly
    # as a non-personalized user sees it — admin QA, and a clean public source
    # (e.g. social auto-posting). It can only DE-privilege the response (strip the
    # operator's positions + personal overlay), never escalate, so it is safe for
    # any caller.
    as_general = request.query_params.get("as") == "general"
    serves_personal = (
        bool(limits.get("personal_report") and limits.get("personal_report") != "none")
        and not as_general
    )
    # When the operator morning report is disabled (general-only mode), the admin
    # is served the shared general report like everyone else — there is no fresh
    # morning report to serve, and the stale last one must not leak.
    import os as _os
    _morning_enabled = _os.environ.get("ENABLE_MORNING_REPORT", "true").lower() in ("true", "1", "yes")
    is_admin_caller = (tier == "admin") and not as_general
    is_operator = is_admin_caller and _morning_enabled

    def _build_result():
        """Blocking DB reads (content_db + OHLCV fallback) — run off the event loop."""
        from carrac.content_db import get_content_db

        db = get_content_db()

        neutralize = False
        # Local view of is_operator: a stale-morning fall-through below
        # downgrades this request to the shared-report path, and the
        # sanitisation + metadata branches must follow it. Reusing the
        # enclosing flag there would have served the operator an UNSANITISED
        # general report — a path that is otherwise unreachable, because the
        # only other way an admin reaches 'general' is
        # ENABLE_MORNING_REPORT=false, which already makes is_operator False.
        operator_view = is_operator
        if is_operator:
            # Bounded by max_age_days: a feature flag must not stand in for a
            # freshness check. When the morning report is stale
            # the operator falls through to the shared 'general' report,
            # which is what everyone else already gets.
            report = db.get_latest_report(
                report_type="morning", max_age_days=_MORNING_MAX_AGE_DAYS
            )
            if not report:
                if db.get_latest_report(report_type="morning") is not None:
                    _stale_morning_alarm("the operator report route")
                report = db.get_latest_report(report_type="general")
                operator_view = False
        else:
            report = db.get_latest_report(report_type="general")
            if not report and serves_personal:
                # Paid tier with no general row yet: serve the morning report
                # through the operator-neutral filter rather than nothing.
                # Bounded too — neutralize() scrubs the operator's
                # positions but NOT the age, so an unbounded fallback would
                # hand paid users June's market read as today's.
                report = db.get_latest_report(
                    report_type="morning", max_age_days=_MORNING_MAX_AGE_DAYS
                )
                if not report:
                    if db.get_latest_report(report_type="morning") is not None:
                        _stale_morning_alarm("the paid-tier existence fallback")
                else:
                    neutralize = True
            # Free tier with no general row stays empty — deliberately NO
            # fallback (the morning report bakes in the operator's positions).
        if not report:
            return None

        # Strip any inline personal section from the shared report content.
        content = report["content"] or ""
        marker = "<!-- PERSONAL_SECTION_START -->"
        idx = content.find(marker)
        if idx != -1:
            content = content[:idx]
        # The one serving policy: the operator gets the row; everyone else
        # gets the general report's public filter, or the operator-neutral cut
        # of the fallback morning report without its summary and narration.
        from carrac.reports.sanitize import report_text_for_viewer

        result: dict[str, Any] = {
            "created_at": report.get("created_at", ""),
            "id": report.get("id"),
            **report_text_for_viewer(
                {**report, "content": content},
                "morning" if (operator_view or neutralize) else "general",
                operator=operator_view,
            ),
        }
        content = result["content"]

        # Per-user personal section, if eligible (tier gate).
        if user_id and serves_personal:
            personal = db.get_personal_section(report["id"], int(user_id))
            if personal and personal.get("content"):
                pc = personal["content"]
                if "<!-- CUSTOMIZE_CTA -->" in pc:
                    result["customize_cta"] = True
                    result["cta_html"] = pc
                elif pc.lstrip().startswith("## The Story") or pc.lstrip().startswith(
                    "## Action"
                ):
                    result["content"] = pc
                    result["is_personalized"] = True
                elif "<!-- CREDITS_EXHAUSTED -->" in pc:
                    result["credits_exhausted"] = True
                else:
                    result["content"] = content + "\n\n" + marker + "\n\n" + pc

        # Report internals (narration, critic review, cost, models, phase
        # results, raw X candidates) are the operator's. A logged-in
        # non-operator gets only the per-source status the report page's
        # degraded-data banner reads.
        meta = None
        if report.get("metadata"):
            try:
                meta = json.loads(report["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        if user_id and isinstance(meta, dict):
            shown = meta if is_admin_caller else _public_report_metadata(meta)
            if shown:
                result["metadata"] = shown

        # x_candidates: expose on the general/non-operator path so downstream
        # consumers (social pipeline, MCP) can surface them without parsing
        # the full metadata blob. Tolerate absence — engine may not yet write it.
        if not operator_view and isinstance(meta, dict):
            candidates = meta.get("x_candidates")
            if isinstance(candidates, list):
                # This is the NON-OPERATOR path, so the same
                # licensing boundary as `content` applies. Candidates stored
                # before the generation gate narrate DVOL/GEX.
                from carrac.reports.sanitize import public_x_candidates
                result["x_candidates"] = public_x_candidates(candidates)

        # Live delta header — compare report-time prices to current.
        if meta and meta.get("report_prices"):
            try:
                from carrac.reports.routes import (
                    _get_cached_crypto_prices,
                    _get_db,
                    _live_price_for_delta,
                )

                report_prices = meta["report_prices"]
                crypto_prices = _get_cached_crypto_prices() or {}
                database = _get_db()
                deltas = []
                for asset, rp in report_prices.items():
                    if rp is None:
                        continue
                    cp = _live_price_for_delta(asset, crypto_prices, database)
                    if cp is not None and rp:
                        pct = (cp - rp) / rp * 100
                        if abs(pct) >= 0.3:
                            deltas.append(
                                {
                                    "asset": asset,
                                    "report_price": rp,
                                    "current_price": cp,
                                    "pct_change": round(pct, 2),
                                }
                            )
                deltas.sort(key=lambda d: abs(d["pct_change"]), reverse=True)
                result["delta_header"] = {
                    "report_time": meta.get(
                        "report_timestamp", report.get("created_at", "")
                    ),
                    "deltas": deltas[:15],
                }
            except Exception:
                pass

        return result

    result = await asyncio.to_thread(_build_result)
    if result is None:
        return JSONResponse({"content": None})

    return JSONResponse(content=jsonable_encoder(result))
