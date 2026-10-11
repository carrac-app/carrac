"""Guards against degenerate / dead-feed z-score anomalies.

Two mechanisms:
- PRIMARY: cap |z|. A MAD-based z of -31 means a near-constant window with one
  blip — not a real anomaly. Left uncapped, a feed like FACTOR_LIQUIDITY (-31.7σ)
  would rank #1 in the daily report feed, crowding out real macro signals.
- SECONDARY: a GENEROUS staleness bound, to drop dead feeds WITHOUT discarding
  legitimately-infrequent macro (PPI is monthly, COT is weekly — those are valid
  even when a couple of weeks old).
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

MAX_ABS_Z = 12.0
MAX_AGE_DAYS = 45


def _as_dt(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_degenerate_anomaly(row: Dict[str, Any], *, max_abs_z: float = MAX_ABS_Z,
                          max_age_days: int = MAX_AGE_DAYS,
                          now: Optional[datetime] = None) -> bool:
    """True when an anomaly row is a degenerate z-score or from a dead feed."""
    z = row.get("zscore")
    if z is not None and abs(float(z)) > max_abs_z:
        return True
    d = _as_dt(row.get("date") or row.get("ts"))
    if d is not None:
        now = now or datetime.now(timezone.utc)
        if (now - d).days > max_age_days:
            return True
    return False


def filter_anomalies(rows: List[Dict[str, Any]], **kw) -> List[Dict[str, Any]]:
    """Drop degenerate/dead-feed rows, preserving order."""
    return [r for r in rows if not is_degenerate_anomaly(r, **kw)]


# An anomaly surviving filter_anomalies() can still be weeks old
# (PPI/COT-style monthly/weekly series are legitimately kept up to
# MAX_AGE_DAYS=45) — filter_anomalies only answers "is this a dead feed?",
# never "is this current?". Both report-side consumers
# (morning_engine/data_assembly.py::fetch_quant_context and
# morning_engine/report_blocks.py::build_anomaly_pulse_block) rendered a
# metric/zscore/percentile line with NO date, so a 21-day-stale JAPAN_10Y
# printed as "active" under an "Active Extremes" header. This is the one
# place that turns a row's date into reader-facing text, so both consumers
# import it rather than re-deriving the same threshold independently.
DISPLAY_STALE_DAYS = 7


def format_anomaly_date_suffix(row_or_date, *, stale_days: int = DISPLAY_STALE_DAYS,
                                now: Optional[datetime] = None) -> str:
    """Render a display suffix carrying an anomaly's date, so a reader (or an
    LLM writing prose from it) can never mistake "active" for "as of today".

    Accepts either an anomaly row dict (looks up 'date' then 'ts') or a raw
    date value. Always includes the date once it is parseable; adds an
    explicit "Nd old" callout when older than ``stale_days``. Returns "" when
    the date is missing/unparseable — deliberately silent, the caller decides
    whether "no date at all" is itself worth flagging.
    """
    date_raw = row_or_date
    if isinstance(row_or_date, dict):
        date_raw = row_or_date.get("date") or row_or_date.get("ts")
    d = _as_dt(date_raw)
    if d is None:
        return ""
    now = now or datetime.now(timezone.utc)
    age_days = (now - d).total_seconds() / 86400.0
    date_str = d.strftime("%Y-%m-%d")
    if age_days > stale_days:
        # Floor, not round. Anomaly dates are date-only, so they parse to
        # midnight and the fractional part is simply "how far into today it
        # is" — with `:.0f` a 23-day-old anomaly reads as "23d old" in the
        # morning and "24d old" after midday, and the afternoon reading is
        # a day wrong. Whole elapsed days is both the correct answer and a
        # stable one. It also avoids a time-of-day flake: the integration test
        # in tests/test_afternoon_weekly_quant_context.py uses the real clock,
        # while the unit test in tests/test_anomaly_filter.py pins `now` to
        # midnight and would never see it.
        return f" (as of {date_str} — {int(age_days)}d old)"
    return f" (as of {date_str})"
