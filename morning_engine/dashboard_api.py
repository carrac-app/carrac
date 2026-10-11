"""
Dashboard API helpers — HTTP calls to the Carrac dashboard.

Covers report save, summary/metadata/audio_script PUTs, personal section
save, eligible user enumeration, per-user analyst source lookup, and the
per-client engine-cost log.

API key is passed via the ``X-Internal-Key`` header (not URL param) so it doesn't
leak to server access logs.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import DASHBOARD_API_KEY, DASHBOARD_BASE_URL

log = logging.getLogger('morning_engine')

# API key via header (not URL param) to avoid leaking in server logs
API_HEADERS = {"X-Internal-Key": DASHBOARD_API_KEY}


def debit_credits(user_id, amount_cents, feature, model,
                  input_tokens=0, output_tokens=0, max_attempts=3):
    """Debit credits with retry + alert on permanent failure.

    Returns True on success, False if all retries exhausted. On exhaustion
    a ntfy/Telegram alert fires and the failure is logged at ERROR (caught
    by the error tracker) — billing reconciliation can replay from the log.
    """
    payload = {
        "user_id": user_id,
        "amount_cents": amount_cents,
        "feature": feature,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                f"{DASHBOARD_BASE_URL}/api/credits/debit",
                json=payload,
                headers=API_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            last_err = e
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))  # 1s, 2s
    log.error(
        "Credit debit FAILED after %d attempts for user=%s feature=%s amount=%dc: %s",
        max_attempts, user_id, feature, amount_cents, last_err,
    )
    try:
        # Lazy import to avoid cycle (notify imports config which imports back)
        from .notify import notify as _notify
        _notify(
            "Credit Debit FAILED",
            f"user={user_id} feature={feature} amount={amount_cents}c err={last_err}",
        )
    except Exception:
        # Notify itself failed — the error tracker already has the ERROR log above.
        pass
    return False


def post_report(content, report_type="morning", assets=None, metadata=None,
                generation_time_ms=0, source_briefing=None):
    """Save report to dashboard via API. Returns the report ID on success.

    source_briefing: the assembled shared/market briefing this
    report was generated from. SHARED reports only (morning/afternoon/
    weekly/week_ahead/general) — never pass a per-user personalized
    briefing here; personal sections go through save_personal_section()
    instead, which has no briefing param by deliberate choice (see that
    function's docstring below).
    """
    url = f"{DASHBOARD_BASE_URL}/api/report/{report_type}"
    payload = {
        "content": content,
        "assets": assets or [],
        "generation_time_ms": generation_time_ms,
    }
    if metadata:
        payload["metadata"] = metadata
    if source_briefing:
        payload["source_briefing"] = source_briefing

    try:
        resp = requests.post(url, json=payload, headers=API_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        report_id = data.get("report_id") or data.get("id")
        log.info("Report saved: #%s (type=%s)", report_id, report_type)
        return report_id
    except Exception as e:
        log.error("Failed to save report: %s", e)
        return None


def _ops_beacon(msg: str) -> None:
    """ERROR-log + Sentry one operational alarm. Seam for tests.

    The failures below (a user's personal report not saving,
    the $30/day cost cap going blind to spend) carried only log.warning —
    the definition of a silence nobody reads. Never raises: these sit in
    the engine's per-user loop and must not stop the remaining reports.
    """
    log.error(msg)
    try:
        import sentry_sdk
        sentry_sdk.capture_message(msg, level='error')
    except Exception:
        pass


def log_engine_costs(report_type, **clients):
    """Record per-client engine costs IN-PROCESS.

    The engine runs inside the app container, so it folds spend straight into
    the global daily-spend counter — so the budget cap + cost alert SEE
    report-generation cost — and logs to ai_usage_log for admin analytics.
    Recording is in-process rather than an HTTP POST with a short timeout and a
    bare ``except: pass``. Failures here are logged, never swallowed.
    """
    for label, client in clients.items():
        if client is None:
            continue
        summary = client.get_usage_summary()
        if summary.get("total_cost", 0) <= 0:
            continue
        cost_cents = max(1, int(summary["total_cost"] * 100))
        # Label by the client's actual model (e.g. claude-sonnet-4-6), not the
        # historical kwarg role name ("opus") — the primary slot runs whatever
        # MORNING_PRIMARY_MODEL selects.
        model = getattr(client, "model", None) or label
        try:
            from user_db import user_db
            user_db.log_ai_usage(
                user_id=0,
                feature=f"engine_{report_type}",
                asset=None,
                input_tokens=summary.get("total_input_tokens", 0),
                output_tokens=summary.get("total_output_tokens", 0),
                cost_cents=cost_cents,
                charged_cents=0,  # platform cost, not charged to a user
                model=model,
            )
        except Exception as e:
            _ops_beacon(f"engine cost usage-log failed ({label}): {e} — "
                        f"{cost_cents}c of real spend is invisible to admin "
                        f"analytics")
        try:
            from carrac.ai import billing
            billing._increment_global_spend(cost_cents)
        except Exception as e:
            _ops_beacon(f"engine global-spend increment failed ({label}): {e} "
                        f"— the daily LLM cost cap is now blind to "
                        f"{cost_cents}c of real spend")


def save_summary(report_id, summary):
    """Save executive summary for a report."""
    try:
        requests.put(
            f"{DASHBOARD_BASE_URL}/api/report/morning/{report_id}/summary",
            json={"summary": summary},
            headers=API_HEADERS,
            timeout=15,
        )
    except Exception as e:
        log.warning("Failed to save summary: %s", e)


def save_metadata(report_id, metadata):
    """Save report metadata (steering config, model info, etc.)."""
    try:
        requests.put(
            f"{DASHBOARD_BASE_URL}/api/report/morning/{report_id}/metadata",
            json=metadata,
            headers=API_HEADERS,
            timeout=15,
        )
    except Exception as e:
        log.warning("Failed to save metadata: %s", e)


def save_audio_script(report_id, audio_script):
    """Save public audio briefing script to dedicated DB column."""
    try:
        requests.put(
            f"{DASHBOARD_BASE_URL}/api/report/morning/{report_id}/audio-script",
            json={"audio_script": audio_script},
            headers=API_HEADERS,
            timeout=15,
        )
    except Exception as e:
        log.warning("Failed to save audio_script: %s", e)


def save_personal_audio(report_id, user_id, audio_data):
    """Upload personal audio MP3 to dashboard."""
    try:
        requests.post(
            f"{DASHBOARD_BASE_URL}/api/report/{report_id}/personal/{user_id}/audio",
            data=audio_data,
            headers={**API_HEADERS, "Content-Type": "audio/mpeg"},
            timeout=30,
        )
    except Exception as e:
        log.warning("Failed to save personal audio for user %s: %s", user_id, e)


def get_eligible_users(region=None, mode="morning"):
    """Get users eligible for personal report sections.

    Returns list of dicts: [{"id": ..., "tier": ..., "email": ..., "region": ...}]
    Eligible: admin, tester, or premium with balance > 0.
    If region is given, only users in that delivery region are returned.
    For mode='week_ahead', also requires user opt-in via prefs.weekly_ahead_enabled.
    """
    try:
        params = {"mode": mode} if mode else {}
        if region:
            params["region"] = region
        resp = requests.get(
            f"{DASHBOARD_BASE_URL}/api/engine/eligible-users",
            params=params,
            headers=API_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("users", [])
    except Exception as e:
        log.warning("Failed to fetch eligible users: %s", e)
        return []


def get_user_analyst_sources(user_id):
    """Get a user's configured analyst sources (X handles + YT channels)."""
    try:
        resp = requests.get(
            f"{DASHBOARD_BASE_URL}/api/engine/user-sources/{user_id}",
            headers=API_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("Failed to fetch analyst sources for user %s: %s", user_id, e)
        return {}


def save_personal_section(report_id, user_id, content, cost_cents=0,
                          input_tokens=0, output_tokens=0, audio_script=None):
    """Save a personal report section via dashboard API.

    Deliberately no source_briefing parameter. The per-user
    briefing that feeds a personal section (trader_profile, plays/thesis,
    positions, alerts — see build_user_briefing() / user_briefing in
    engine.py's _generate_one_user_report) is materially more sensitive
    than the shared market briefing persisted for the operator/general/
    afternoon/weekly/week_ahead reports, and is already treated as
    untrusted content (carrac/grounding/escape.py). The debugging value of
    persisting it doesn't outweigh storing a user's book in a second place
    with its own retention story. Do not add a briefing param here without
    revisiting that decision explicitly.
    """
    try:
        payload = {
            "user_id": user_id,
            "content": content,
            "cost_cents": cost_cents,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        if audio_script:
            payload["audio_script"] = audio_script
        resp = requests.post(
            f"{DASHBOARD_BASE_URL}/api/report/{report_id}/personal",
            json=payload,
            headers=API_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        _ops_beacon(f"personal report save FAILED for user {user_id} "
                    f"(report {report_id}): {e} — the user gets no personal "
                    f"report this run and nothing else will say so")
