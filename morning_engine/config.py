"""Morning engine configuration — assets, models, defaults."""

import os
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Grok API (retained for x_search only)
# ---------------------------------------------------------------------------
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE_URL = "https://api.x.ai/v1"
GROK_MODEL = "grok-4-1-fast-reasoning"
GROK_MODEL_NON_REASONING = "grok-4-1-fast-non-reasoning"

# ---------------------------------------------------------------------------
# Anthropic API (primary model for all generation)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6")
ANTHROPIC_MODEL_FAST = os.environ.get("ANTHROPIC_MODEL_FAST", "claude-sonnet-4-6")

# ---------------------------------------------------------------------------
# Dashboard API (local when running next to the app)
# ---------------------------------------------------------------------------
DASHBOARD_BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "http://localhost:5000")
DASHBOARD_API_KEY = os.environ.get("SCREENSHOT_API_KEY", "")

# ---------------------------------------------------------------------------
# Supadata (YouTube transcripts)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Chart pairs — moved to carrac.assets.registry (single source of truth).
# CHART_PAIRS is kept as a property-style accessor for back-compat with any
# external import.
# ---------------------------------------------------------------------------
def _chart_pairs():
    from carrac.assets.registry import chart_pairs as _cp
    return _cp()


# Module-level constant for back-compat with code that does `from .config import CHART_PAIRS`.
# Resolved at import time; admin add/remove updates the registry directly so
# code paths that re-fetch (most morning_engine) see updates without restart.
CHART_PAIRS = _chart_pairs()

# X accounts the macro-research Grok call is restricted to. Comma-separated
# handles in X_SEARCH_HANDLES; empty (the default) means no x_search at all,
# which engine.py logs loudly rather than searching the whole of X.
DEFAULT_X_HANDLES = [
    h.strip().lstrip("@")
    for h in os.environ.get("X_SEARCH_HANDLES", "").split(",")
    if h.strip()
]


# ---------------------------------------------------------------------------
# TTS (Text-to-Speech) settings
# ---------------------------------------------------------------------------
TTS_ENABLED = os.environ.get("TTS_ENABLED", "true").lower() in ("true", "1", "yes")
TTS_VOICE_ID = os.environ.get("TTS_VOICE_ID", "rex")  # rex=authoritative, leo=commanding

# ---------------------------------------------------------------------------
# Report settings
# ---------------------------------------------------------------------------
MAX_REPORT_TOKENS = 4500  # Shared/free-tier report: target 10-14K chars
ENABLE_CRITIC = os.environ.get("ENABLE_CRITIC", "true").lower() in ("true", "1", "yes")
MAX_MACRO_TOKENS = 4000
MAX_PERSONAL_TOKENS = 16000  # Legacy personal sections: target 10-14K chars

# Per-user personalized reports (feature flag)
PERSONALIZED_REPORTS = os.environ.get("PERSONALIZED_REPORTS", "false").lower() in ("true", "1")
# Free-tier shared "general" report: one lightweight, operator-neutral report per
# cycle, served to ALL free accounts (fixed operator cost regardless of user count).
# Generated alongside the morning report from the shared (non-operator) briefing.
ENABLE_GENERAL_REPORT = os.environ.get("ENABLE_GENERAL_REPORT", "true").lower() in ("true", "1", "yes")
# Operator-only "morning" report (admin's positions/alerts/plays woven in). When
# OFF, the dispatch run skips the morning report entirely (generating only the
# free general report), the operator is served the general report, and the
# operator-scoped data fetches (plays/alerts/positions/history) that 401 without
# a user context are skipped. Default ON to preserve existing behavior.
ENABLE_MORNING_REPORT = os.environ.get("ENABLE_MORNING_REPORT", "true").lower() in ("true", "1", "yes")


def reports_paused() -> bool:
    """True when NO automated report is produced any more (both the general and
    the operator report are switched off). Read from the environment at call
    time, not at import, so the public pages flip the moment the containers are
    recreated with the new env — no code change needed to pause or resume.

    The report crons are gone and ENABLE_GENERAL_REPORT
    is false in production; /brief, /brief/archive, the landing card and the guide
    must say so instead of presenting the last stored report as today's."""
    def _on(name):
        return os.environ.get(name, "true").lower() in ("true", "1", "yes")
    return not _on("ENABLE_GENERAL_REPORT") and not _on("ENABLE_MORNING_REPORT")
# BETA: decouple the free-tier "general" report from the per-region eligible-users
# gate. run_dispatch normally early-returns for a region with 0 eligible users —
# which means the operator-neutral general report only gets produced in regions
# that happen to have paid/eligible users. With this flag on, a region with no
# eligible users still runs the shared pipeline to produce the general report
# (the per-user phase then no-ops), guarded by a once-per-day idempotency check.
# Default OFF so the deployed code is inert until explicitly enabled; set
# GENERAL_REPORT_UNGATED=true for beta, unset/false to restore the gated behavior
# once paying customers (with real per-region eligibility) arrive.
GENERAL_REPORT_UNGATED = os.environ.get("GENERAL_REPORT_UNGATED", "false").lower() in ("true", "1", "yes")
MAX_FREE_REPORT_TOKENS = 5500     # Free tier: ~12K-char report (~2.3 chars/tok) with headroom; trim_dangling_sentence() covers a cut-off run.
MAX_USER_REPORT_TOKENS = 7000     # Premium/admin: full personalized report (14-18K-char target); trim_dangling_sentence() covers a cut-off run.
MAX_USER_REPORT_TOKENS_MEMBER = 3000  # Member tier: lighter report (~10K chars)

# Notification targets
# No default. The topic is a bearer credential on an
# unauthenticated ntfy instance; a committed default is a published one.
# Supplied by the deployment .env (containers) and the crontab header
# (host crons). Empty means alerting is off, and ntfy.py says so.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
# Audience split: alarms (guards, tripwires, verifier verdicts) go to the
# Carrac topic; the market digests read like a feed and go to the market one.
# Neither falls back to NTFY_TOPIC — see carrac/config.py for why.
NTFY_CARRAC_TOPIC = os.environ.get("NTFY_CARRAC_TOPIC", "")
NTFY_MARKET_TOPIC = os.environ.get("NTFY_MARKET_TOPIC", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def get_freshness_window():
    """Return appropriate time window description based on day of week."""
    dow = datetime.now().weekday()
    if dow == 0:
        return "over the weekend and Monday pre-market"
    elif dow >= 5:
        return "since Friday close"
    else:
        return "in the last 24 hours"


def is_weekend():
    # CPR/regime calculations are on UTC bars; weekend check must match.
    return datetime.now(timezone.utc).weekday() >= 5


def is_first_trading_day_of_month():
    # CPR pivots roll over on the UTC calendar; using local time would
    # mis-flag the first weekday for users west of UTC (e.g. NY at 21:00 ET
    # is already next-day UTC). Force UTC to stay aligned with chart bars.
    today = datetime.now(timezone.utc)
    if today.day <= 3 and today.weekday() < 5:
        # Check if there was a weekday before today this month
        for d in range(1, today.day):
            check = today.replace(day=d)
            if check.weekday() < 5:
                return False
        return True
    return False
