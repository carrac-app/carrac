"""Canonical resolution of "the previous published morning-equivalent report".

Several places need "the last morning read" to build day-over-day
continuity — the ``## Yesterday`` prompt section that ``run_dispatch`` and
``run_morning`` render, and the ``/api/engine/morning-context`` preassembly
that feeds them. Every one of them asked for it as::

    get_latest_report(report_type='morning')

with no date bound. ``report_type='morning'`` stops being written when
ENABLE_MORNING_REPORT is false, after which every caller would silently
receive the same old document, label it "Yesterday", and hand it to an LLM as
day-over-day ground truth, unnoticed because nothing checks the age.

The report_type question must be answered ONCE, in one function, or the
callers drift apart. This module is that one place:

  * ``general`` is the live morning-equivalent report today — it writes daily
    and is what a reader actually sees. It is preferred.
  * ``morning`` is the legacy type, kept as a fallback purely so this keeps
    working if ENABLE_MORNING_REPORT is ever turned back on.
  * Both are age-bounded, so a stale row cannot masquerade as current.
  * When neither resolves, the caller gets None **and a human gets paged** —
    this condition must never be silent.
"""

import logging

logger = logging.getLogger(__name__)

# How old the prior-day baseline may be before it stops counting as one.
#
# Both candidate types generate daily including weekends, so a healthy value here is <1. Three
# days is deliberately slack: scripts/report-health-check.sh already owns the
# "a daily report is missing" alarm at 30h, and this bound exists to catch the
# qualitatively different failure it missed — a report type that stopped
# writing MONTHS ago still being served as current. Setting it tight enough to
# double-report the 30h condition would just add noise to an alarm that
# already fires (do not add an alarm nobody will read).
PRIOR_DAY_MAX_AGE_DAYS = 3.0

# Preference order. First fresh hit wins; see the module docstring.
_CANDIDATE_TYPES = ('general', 'morning')


def _capture(message: str) -> None:
    """Send one message to Sentry. Seam so the alarm is testable.

    Same shape as carrac/reports/afternoon_grounding.py::_capture — the
    established human-reaching alarm in this package.
    """
    import sentry_sdk
    sentry_sdk.capture_message(message, level="error")


def _alarm(message: str) -> None:
    logger.error("[prior-day] %s", message)
    try:
        _capture(message)
    except Exception:  # noqa: BLE001
        # logger.error above already fired — this is not a silent loss.
        logger.warning("prior-day alarm: Bugsink capture failed", exc_info=True)


def resolve_prior_day_report(db=None, max_age_days: float = PRIOR_DAY_MAX_AGE_DAYS):
    """Return the most recent morning-equivalent report, or None.

    None means "no current baseline exists" and is a real answer — callers
    MUST render it as an explicit absence ("No previous report") and never
    substitute an older document. Returning None also alarms, because a
    missing day-over-day baseline is exactly the condition that hid for six
    weeks.

    Returns the report dict with an added ``report_type_used`` key so callers
    and logs can see which type answered.
    """
    if db is None:
        from carrac.content_db import get_content_db
        db = get_content_db()

    stale_seen = []
    for rtype in _CANDIDATE_TYPES:
        try:
            report = db.get_latest_report(report_type=rtype, max_age_days=max_age_days)
        except Exception:  # noqa: BLE001
            logger.warning("prior-day: %s lookup failed", rtype, exc_info=True)
            continue
        if report:
            report = dict(report)
            report['report_type_used'] = rtype
            return report
        # Distinguish "type never wrote" from "type wrote, but long ago" so
        # the alarm below can say something actionable instead of "missing".
        try:
            unbounded = db.get_latest_report(report_type=rtype)
        except Exception:  # noqa: BLE001
            unbounded = None
        if unbounded:
            stale_seen.append(f"{rtype}={unbounded.get('created_at')}")

    detail = ("; newest rows: " + ", ".join(stale_seen)) if stale_seen else \
             "; no rows of any candidate type exist"
    _alarm(
        "prior-day-missing: no prior-day report within %.0fd for any of %s%s. "
        "Day-over-day continuity is running blind — the '## Yesterday' section "
        "will render as absent."
        % (max_age_days, "/".join(_CANDIDATE_TYPES), detail)
    )
    return None
