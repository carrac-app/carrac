"""
Admin System-Health Check Framework
====================================

Provides a registry of green/amber/red health checks covering all critical
Carrac subsystems. The design goal is "silent correctness" visibility — catching
signals that quietly stop working before a beta user notices.

Usage:
    from carrac.admin.health_checks import run_all_checks
    results = run_all_checks()   # list[dict], cached ~30s

Check contract:
    Every registered function must:
    - Return a dict: {status, detail}  (status in GREEN/AMBER/RED)
    - Catch its own exceptions and return RED with the message
    - Never mutate production state (read-only)
    - Complete within ~3 s (individual check budget)
"""

from __future__ import annotations

import logging
import os
import time
import threading
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("carrac.admin.health")

# ── Status constants ──────────────────────────────────────────────────────────

GREEN = "green"
AMBER = "amber"
RED   = "red"

# ── Registry ──────────────────────────────────────────────────────────────────

_REGISTRY: List[Dict] = []


def register_check(component: str, category: str):
    """Decorator that registers a check function into the global registry.

    Usage:
        @register_check("database", "infrastructure")
        def check_database():
            ...
            return {"status": GREEN, "detail": "ok"}
    """
    def decorator(fn: Callable) -> Callable:
        _REGISTRY.append({
            "component": component,
            "category":  category,
            "fn":        fn,
        })
        return fn
    return decorator


# ── Result cache (~30 s) ──────────────────────────────────────────────────────

_cache_lock   = threading.Lock()
_cache_result: Optional[List[dict]] = None
_cache_ts:     float                = 0.0
_CACHE_TTL:    float                = 30.0          # seconds


def run_all_checks(force: bool = False) -> List[dict]:
    """Execute every registered check and return results.

    Results are cached for ~30 s.  Pass force=True to bypass the cache
    (useful for the JSON API endpoint when called with ?refresh=1).
    """
    global _cache_result, _cache_ts

    with _cache_lock:
        if not force and _cache_result is not None:
            if (time.monotonic() - _cache_ts) < _CACHE_TTL:
                return _cache_result

    results = []
    for entry in _REGISTRY:
        component = entry["component"]
        category  = entry["category"]
        fn        = entry["fn"]

        t0 = time.monotonic()
        try:
            outcome = fn()
        except Exception as exc:             # belt-and-suspenders; checks must not raise
            logger.warning("Health check %r raised unexpectedly: %s", component, exc)
            outcome = {"status": RED, "detail": f"check raised: {exc}"}

        latency_ms = int((time.monotonic() - t0) * 1000)

        results.append({
            "component":  component,
            "category":   category,
            "status":     outcome.get("status", RED),
            "detail":     outcome.get("detail", ""),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": latency_ms,
        })

    with _cache_lock:
        _cache_result = results
        _cache_ts     = time.monotonic()

    return results


# =============================================================================
# Checks — Infrastructure
# =============================================================================

@register_check("database", "infrastructure")
def check_database() -> dict:
    """PostgreSQL connectivity: getconn + SELECT 1 latency.

    Amber threshold: > 200 ms (pool pressure or slow query planner).
    Red:             any exception (pool exhausted, postgres down).
    Also reports pool in-use / size when available.
    """
    try:
        from carrac.db_pool import get_conn, get_db_type, _main_pool

        t0 = time.monotonic()
        with get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        latency_ms = int((time.monotonic() - t0) * 1000)

        db_type = get_db_type()
        detail_parts = [f"type={db_type}", f"SELECT 1 in {latency_ms} ms"]

        # Pool stats (PostgreSQL only — psycopg2 ThreadedConnectionPool)
        if db_type == "postgresql" and _main_pool is not None:
            try:
                pool_used = len(_main_pool._used)
                pool_size = _main_pool.maxconn
                detail_parts.append(f"pool {pool_used}/{pool_size}")
            except Exception:
                pass

        status = AMBER if latency_ms > 200 else GREEN
        return {"status": status, "detail": ", ".join(detail_parts)}

    except Exception as exc:
        return {"status": RED, "detail": f"DB error: {exc}"}


@register_check("redis", "infrastructure")
def check_redis() -> dict:
    """Redis ping + response-cache freshness.

    Green:  ping ok, HTF cache age < 40 min.
    Amber:  ping ok but HTF cache stale > 40 min (precompute may have skipped).
    Red:    Redis unavailable.
    """
    try:
        from carrac.redis_cache import get_redis, ResponseCache

        r = get_redis()
        if r is None:
            return {"status": RED, "detail": "Redis unavailable (client is None)"}

        t0 = time.monotonic()
        r.ping()
        ping_ms = int((time.monotonic() - t0) * 1000)

        # Cache freshness
        cache = ResponseCache()
        stats = cache.get_stats()
        htf   = stats.get("htf", {})
        htf_age = htf.get("age_seconds")

        detail = f"ping {ping_ms} ms"
        if htf_age is not None:
            detail += f", HTF cache age {htf_age}s"
            if not htf.get("fresh") and htf_age > 2400:   # 40 min
                return {"status": AMBER, "detail": detail + " (cache stale)"}
        else:
            detail += ", HTF cache absent"
            return {"status": AMBER, "detail": detail}

        return {"status": GREEN, "detail": detail}

    except Exception as exc:
        return {"status": RED, "detail": f"Redis error: {exc}"}


# =============================================================================
# Checks — Worker
# =============================================================================

@register_check("worker", "infrastructure")
def check_worker() -> dict:
    """Background worker heartbeat age.

    Green:  heartbeat < 10 min old.
    Amber:  10–15 min  (one cycle missed but possibly mid-precompute).
    Red:    > 15 min or missing heartbeat key.
    """
    import json as _json

    try:
        from carrac.caches import _get_redis

        r = _get_redis()
        if r is None:
            return {"status": AMBER, "detail": "Redis unavailable — cannot check heartbeat"}

        raw = r.get("mcd:worker:heartbeat")
        if not raw:
            return {"status": RED, "detail": "Heartbeat key absent — worker may be down"}

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        payload = _json.loads(raw)
        ts = datetime.fromisoformat(payload["ts"])
        age_sec = int((datetime.now() - ts).total_seconds())

        if age_sec <= 600:
            return {"status": GREEN, "detail": f"heartbeat {age_sec}s ago"}
        if age_sec <= 900:
            return {"status": AMBER, "detail": f"heartbeat {age_sec}s ago (1 missed cycle)"}
        return {"status": RED, "detail": f"heartbeat {age_sec}s ago (worker stalled?)"}

    except Exception as exc:
        return {"status": RED, "detail": f"Heartbeat check error: {exc}"}


# =============================================================================
# Checks — Indicators (silent-correctness)
# =============================================================================

@register_check("indicators", "correctness")
def check_indicators() -> dict:
    """End-to-end indicator vocab check — the "silent correctness" sentinel.

    For each indicator in INDICATOR_ALERT_CONDITIONS (alerts vocab) and
    CONDITION_SCHEMAS['indicator_state'] (plays vocab), verify that:

    1. Both surfaces agree on which indicators are alertable.
    2. For each shared indicator the vocab sets are identical
       (drift → alert fires but play never matches, or vice versa).

    Uses the precomputed HTF cache as canary: picks the first non-metric
    asset and runs _get_indicator_state + indicator_state extract_fingerprint
    to confirm both paths return in-vocabulary values.

    Status:
      green = all checked indicators produce in-vocabulary states from cache.
      amber = some indicators return None (not computable) or vocab drift.
      red   = cache unavailable, or >50% indicators failing.
    """
    try:
        from carrac.indicator_state import (
            INDICATOR_ALERT_CONDITIONS,
            TRACKED_STATES,
            extract_fingerprint,
        )
        from carrac.conditions import _get_indicator_state as _cond_get_state

        # ── 1. Vocab cross-check (static, no cache needed) ────────────────
        # alerts vocab = keys in INDICATOR_ALERT_CONDITIONS
        # plays  vocab = keys handled by _get_indicator_state (infer from TRACKED_STATES)
        alerts_indicators = set(INDICATOR_ALERT_CONDITIONS.keys())

        # Map TRACKED_STATES keys to the indicator names _get_indicator_state handles.
        # The naming convention used by conditions.py is uppercase with _ separator.
        # Build a set of what _get_indicator_state recognises by inspecting TRACKED_STATES keys.
        tracked_keys = {k for k, _, _ in TRACKED_STATES}

        # Indicators that are in alerts vocab but NOT in tracked states are vocab-drift.
        # (tracked_keys use e.g. 'ema_ribbon'; alert keys use same snake_case)
        missing_in_tracked  = alerts_indicators - tracked_keys
        extra_in_tracked    = tracked_keys - alerts_indicators

        # Alert vocab vs conditions.py vocab: check that all alert-advertised states
        # are actually producible. We do a lightweight canary: build a known synthetic
        # asset dict and confirm _get_indicator_state returns something in-vocab.
        # NOTE: the Plays evaluator (_get_indicator_state) intentionally uses a
        # DIFFERENT vocabulary than the Alerts tracked-states for some indicators
        # (e.g. STRUCTURE plays = HH_HL/LH_LL/range vs alerts = bullish/bearish/
        # neutral; super_trend plays may add 'neutral'). That is by design, NOT
        # drift — so we do NOT compare plays output against the alerts vocab.
        # The real silent-failure signals are (a) an alerts-advertised indicator
        # with no tracked state (wiring gap, below) and (b) the alerts producer
        # emitting a state its own vocab rejects (canary, below).
        vocab_drift: List[str] = sorted(missing_in_tracked)

        # ── 2. Cache canary ───────────────────────────────────────────────
        cache_ok     = False
        green_count  = 0
        null_count   = 0
        bad_count    = 0
        amber_names: List[str] = []
        bad_names:   List[str] = []

        try:
            from carrac.redis_cache import ResponseCache
            rc = ResponseCache()
            cached = rc.get("htf")
            if cached:
                # Pick first non-metric asset as canary
                canary_asset = None
                for asset in cached.get("assets", []):
                    if asset.get("type") not in ("metric", "crypto_metric"):
                        canary_asset = asset
                        break

                if canary_asset:
                    cache_ok = True
                    fp       = extract_fingerprint(canary_asset)

                    for ind, valid_states in INDICATOR_ALERT_CONDITIONS.items():
                        state = fp.get(ind)
                        cond_state = _cond_get_state(ind.upper(), canary_asset)

                        if state is None and cond_state is None:
                            null_count += 1
                        elif state is not None and str(state) not in valid_states:
                            # Alerts producer emits a state its OWN vocab rejects →
                            # that alert transition can never match. Real silent bug.
                            bad_count += 1
                            bad_names.append(f"{ind}={state}")
                        elif state is None or cond_state is None:
                            # One-sided: only one evaluator has data for this indicator.
                            amber_names.append(ind)
                        else:
                            green_count += 1
        except Exception as cache_exc:
            logger.debug("Indicator check: cache canary failed: %s", cache_exc)

        # ── 3. Summarise ──────────────────────────────────────────────────
        total = len(INDICATOR_ALERT_CONDITIONS)
        parts = []

        if vocab_drift:
            parts.append(f"vocab drift: {', '.join(vocab_drift[:3])}")
        if bad_names:
            parts.append(f"bad states: {', '.join(bad_names[:5])}")
        if amber_names:
            parts.append(f"one-sided: {', '.join(amber_names[:5])}")
        if missing_in_tracked:
            parts.append(f"not in TRACKED_STATES: {', '.join(sorted(missing_in_tracked))}")

        if cache_ok:
            parts.insert(0, f"cache canary: {green_count}/{total} green, {null_count} null, {bad_count} bad")
        else:
            parts.insert(0, "cache not available — static checks only")

        detail = "; ".join(parts) if parts else f"all {total} indicators ok"

        if bad_count > total * 0.5 or vocab_drift or missing_in_tracked:
            return {"status": RED, "detail": detail}
        if bad_count > 0 or amber_names or extra_in_tracked:
            return {"status": AMBER, "detail": detail}
        return {"status": GREEN, "detail": detail}

    except Exception as exc:
        return {"status": RED, "detail": f"Indicator check error: {exc}"}


# =============================================================================
# Checks — Data sources
# =============================================================================

@register_check("data_sources", "correctness")
def check_data_sources() -> dict:
    """HTF/LTF response cache freshness — main market data pipeline.

    Green:  HTF age < 35 min, asset count > 0.
    Amber:  HTF age 35–90 min (precompute running late).
    Red:    HTF absent or age > 90 min.
    """
    try:
        from carrac.redis_cache import ResponseCache

        cache = ResponseCache()
        stats = cache.get_stats()
        htf   = stats.get("htf", {})

        if not htf.get("cached"):
            return {"status": RED, "detail": "HTF response cache absent"}

        age_sec     = htf.get("age_seconds", 0) or 0
        asset_count = htf.get("asset_count", 0)
        age_min     = age_sec // 60

        detail = f"HTF cache {age_min}m old, {asset_count} assets"

        if asset_count == 0:
            return {"status": RED, "detail": detail + " (no assets!)"}
        if age_sec > 5400:   # 90 min
            return {"status": RED, "detail": detail + " (very stale)"}
        if age_sec > 2100:   # 35 min
            return {"status": AMBER, "detail": detail + " (precompute late?)"}

        return {"status": GREEN, "detail": detail}

    except Exception as exc:
        return {"status": RED, "detail": f"Data sources check error: {exc}"}


# =============================================================================
# Checks — Morning engine
# =============================================================================

@register_check("morning_engine", "correctness")
def check_morning_engine() -> dict:
    """Latest morning report age.

    Green:  last report < 30 h ago.
    Amber:  30–48 h  (one missed run).
    Red:    > 48 h or no report ever.

    Skipped (green) when ENABLE_MORNING_REPORT=false — the engine intentionally
    skips Phase C, so an ageing morning report is expected, not a stall.
    """
    if os.environ.get("ENABLE_MORNING_REPORT", "true").lower() not in ("true", "1", "yes"):
        return {"status": GREEN, "detail": "morning report disabled (ENABLE_MORNING_REPORT=false)"}
    try:
        from carrac.content_db import get_content_db

        db     = get_content_db()
        report = db.get_latest_report(report_type="morning")

        if not report:
            return {"status": AMBER, "detail": "No morning reports found (new deploy?)"}

        created_raw = report.get("created_at", "")
        if not created_raw:
            return {"status": AMBER, "detail": "Report found but no created_at timestamp"}

        # Parse ISO string or datetime
        if isinstance(created_raw, datetime):
            created = created_raw.replace(tzinfo=None)
        else:
            try:
                created = datetime.fromisoformat(str(created_raw).replace("Z", ""))
            except (ValueError, TypeError):
                return {"status": AMBER, "detail": f"Cannot parse created_at: {created_raw!r}"}

        age_sec = (datetime.utcnow() - created).total_seconds()
        age_h   = age_sec / 3600
        detail  = f"last report {age_h:.1f}h ago (id={report.get('id')})"

        # Morning reports run Mon–Fri only (crontab `* * 1-5`), so the last
        # report legitimately ages across the weekend (Fri-morning → Mon-morning
        # ≈ 72h). Weekend-tolerant thresholds avoid a false RED every week.
        is_weekend = datetime.utcnow().weekday() >= 5  # 5=Sat, 6=Sun
        if age_h <= 30:
            return {"status": GREEN, "detail": detail}
        if age_h <= 90:
            note = " (weekend — weekday-only schedule)" if is_weekend else " (1+ missed run)"
            return {"status": AMBER, "detail": detail + note}
        return {"status": RED, "detail": detail + " (engine likely stalled)"}

    except Exception as exc:
        return {"status": RED, "detail": f"Morning engine check error: {exc}"}


# Per-type report cadence (hours) → AMBER above `amber_h`, RED above `red_h`.
# Daily types are weekend-tolerant; weekly/week_ahead run once a week so a 7-day
# age is normal — only flag when a full extra cycle has been missed.
_REPORT_CADENCE = {
    "general":    {"amber_h": 30,  "red_h": 54,  "label": "general (daily)"},
    "afternoon":  {"amber_h": 30,  "red_h": 54,  "label": "afternoon (daily)"},
    "weekly":     {"amber_h": 192, "red_h": 240, "label": "weekly (Fri)"},
    "week_ahead": {"amber_h": 192, "red_h": 240, "label": "week-ahead (Sun)"},
}


@register_check("report_pipeline", "correctness")
def check_report_pipeline() -> dict:
    """Freshness of the non-morning report types (morning has its own check).

    One tile for the whole pipeline; reports the worst status and names every
    stale type so a silently-dead afternoon/weekly cron surfaces here.
    """
    try:
        from carrac.content_db import get_content_db

        db = get_content_db()
        now = datetime.utcnow()
        worst = GREEN
        parts = []
        for rtype, cad in _REPORT_CADENCE.items():
            report = db.get_latest_report(report_type=rtype)
            if not report or not report.get("created_at"):
                # Absent isn't RED on its own — a type may simply never have run
                # yet on a fresh deploy. Mark AMBER so it's visible, not alarming.
                parts.append(f"{cad['label']}: none")
                worst = AMBER if worst == GREEN else worst
                continue
            created_raw = report["created_at"]
            if isinstance(created_raw, datetime):
                created = created_raw.replace(tzinfo=None)
            else:
                try:
                    created = datetime.fromisoformat(str(created_raw).replace("Z", ""))
                except (ValueError, TypeError):
                    parts.append(f"{cad['label']}: bad ts")
                    worst = AMBER if worst == GREEN else worst
                    continue
            age_h = (now - created).total_seconds() / 3600
            if age_h > cad["red_h"]:
                status = RED
            elif age_h > cad["amber_h"]:
                status = AMBER
            else:
                status = GREEN
            parts.append(f"{cad['label']}: {age_h:.0f}h{'' if status == GREEN else ' ⚠'}")
            if status == RED or (status == AMBER and worst != RED):
                worst = status
        return {"status": worst, "detail": " · ".join(parts)}
    except Exception as exc:
        return {"status": RED, "detail": f"Report pipeline check error: {exc}"}


@register_check("errors", "errors")
def check_errors() -> dict:
    """Error-tracking signal: error-tracker reachability + recent CSP violations.

    The error tracker has no read API exposed to the app, so this surfaces a reachable/
    unreachable signal plus the in-process CSP-violation counter rather than a
    full event feed (open the error tracker via the SSH tunnel for individual events).
    """
    try:
        import urllib.request

        from carrac.config import Config

        bits = []
        status = GREEN

        # The error tracker reachability (only meaningful when a DSN is configured).
        dsn = os.getenv("SENTRY_DSN") or getattr(Config, "SENTRY_DSN", "") or ""
        if dsn:
            try:
                # DSN host:port is the ingest endpoint; a TCP/HTTP touch tells us
                # the receiver is up. Hitting "/" is cheap and unauthenticated.
                host = dsn.split("@", 1)[1].split("/", 1)[0] if "@" in dsn else "bugsink:8000"
                urllib.request.urlopen(f"http://{host}/", timeout=2)
                bits.append("bugsink: up")
            except Exception as be:
                bits.append(f"bugsink: unreachable ({type(be).__name__})")
                status = AMBER
        else:
            bits.append("bugsink: no DSN")

        # Recent CSP violations (module-level counter in carrac.admin.routes).
        try:
            from carrac.admin import routes as _admin_routes
            csp = int(getattr(_admin_routes, "_csp_violation_count", 0))
            bits.append(f"csp violations: {csp}")
            if csp > 50:
                status = AMBER if status == GREEN else status
        except Exception:
            bits.append("csp violations: n/a")

        return {"status": status, "detail": " · ".join(bits)}
    except Exception as exc:
        return {"status": RED, "detail": f"Errors check error: {exc}"}


# =============================================================================
# Checks — AI subsystem
# =============================================================================

@register_check("ai", "ai")
def check_ai() -> dict:
    """Global AI daily budget + grounding flag.

    Green:  > 25% budget remaining AND grounding enabled.
    Amber:  10–25% remaining OR grounding disabled.
    Red:    < 10% budget OR grounding flag is explicitly off.
    """
    try:
        from carrac.ai.billing import get_global_ai_spend_cents_today
        from carrac.ai.billing import _daily_cap_cents

        cap_cents   = _daily_cap_cents()
        spent_cents = get_global_ai_spend_cents_today()
        remaining   = cap_cents - spent_cents
        pct_left    = (remaining / cap_cents * 100) if cap_cents > 0 else 100

        grounding_ok = os.environ.get("GROUNDING_ENABLED", "1") != "0"

        detail = (
            f"budget ${spent_cents/100:.2f}/${cap_cents/100:.2f} "
            f"({pct_left:.0f}% left), grounding={'on' if grounding_ok else 'OFF'}"
        )

        if not grounding_ok:
            return {"status": RED, "detail": detail + " — GROUNDING_ENABLED=0"}
        if pct_left < 10:
            return {"status": RED, "detail": detail + " (budget nearly exhausted)"}
        if pct_left < 25:
            return {"status": AMBER, "detail": detail + " (budget low)"}
        return {"status": GREEN, "detail": detail}

    except Exception as exc:
        return {"status": RED, "detail": f"AI check error: {exc}"}


# =============================================================================
# Checks — Billing (Paddle)
# =============================================================================

@register_check("billing", "billing")
def check_billing() -> dict:
    """Paddle key presence + webhook inbox backlog.

    Green:  both keys set, inbox backlog = 0 unprocessed/error.
    Amber:  payments intentionally off (no keys — beta), OR inbox backlog > 0.
    Red:    keys partially set (misconfigured) or a real billing fault.

    Note: payments-off is AMBER not RED so the overall health endpoint stays
    200 for Kuma/Watchdog during beta — RED is reserved for a billing system
    that is supposed to work but is broken.
    """
    try:
        from carrac.config import Config

        webhook_secret = Config.PADDLE_WEBHOOK_SECRET
        api_key        = Config.PADDLE_API_KEY

        if not webhook_secret and not api_key:
            # Both absent = payments deliberately disabled (beta). Not an alarm.
            return {"status": AMBER, "detail": "payments disabled (no Paddle keys — beta)"}
        if not webhook_secret or not api_key:
            # Exactly one set = a real misconfiguration worth alarming on.
            missing = "PADDLE_WEBHOOK_SECRET" if not webhook_secret else "PADDLE_API_KEY"
            return {"status": RED, "detail": f"Paddle misconfigured — missing {missing}"}

        # Check inbox backlog (PostgreSQL only — table exists only in production schema)
        backlog = 0
        try:
            from carrac.db_pool import get_conn, get_db_type
            if get_db_type() == "postgresql":
                with get_conn() as conn:
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM paddle_webhook_inbox "
                        "WHERE processed_at IS NULL"
                    ).fetchone()
                    if row:
                        backlog = int(row["cnt"] if hasattr(row, "__getitem__") else row[0])
        except Exception as inbox_exc:
            # Table may not exist in SQLite dev mode — treat as zero
            logger.debug("Paddle inbox check skipped: %s", inbox_exc)

        detail = f"keys configured, inbox backlog={backlog}"
        if backlog > 0:
            return {"status": AMBER, "detail": detail + f" ({backlog} unprocessed events)"}
        return {"status": GREEN, "detail": detail}

    except Exception as exc:
        return {"status": RED, "detail": f"Billing check error: {exc}"}


# =============================================================================
# Checks — MCP
# =============================================================================

@register_check("mcp", "api")
def check_mcp() -> dict:
    """MCP tool registry — verifies tools are loaded.

    Green:  > 0 tools registered.
    Amber:  registry is empty (tools/__init__.py not imported yet?).
    Red:    import fails entirely.
    """
    try:
        # Trigger tool module loading (idempotent; imports are cached)
        import carrac.mcp.tools  # noqa: F401 — side-effect: populates TOOL_REGISTRY
        from carrac.mcp.registry import TOOL_REGISTRY

        count = len(TOOL_REGISTRY)
        detail = f"{count} tools registered"
        if count == 0:
            return {"status": AMBER, "detail": detail + " (registry empty — import issue?)"}
        return {"status": GREEN, "detail": detail}

    except Exception as exc:
        return {"status": RED, "detail": f"MCP check error: {exc}"}
