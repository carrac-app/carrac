"""
Macro event calendar collector for Quant DB.

Hardcoded FOMC, CPI, NFP, ECB, BoJ dates for 2026 and 2027.
Stores in quant.events table.

Usage:
    python -m quant.collectors.event_calendar
    python -m quant.collectors.event_calendar --dry-run

2027 source verification:
  FOMC:  https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm  — CONFIRMED
  ECB:   https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html — CONFIRMED
  CPI:   https://www.bls.gov/schedule/news_release/cpi.htm  — NOT YET PUBLISHED (BLS publishes ~Oct)
  NFP:   https://www.bls.gov/schedule/news_release/empsit.htm — NOT YET PUBLISHED (BLS publishes ~Oct)
  BoJ:   https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm — NOT YET PUBLISHED
When BLS and BoJ publish their 2027 schedules, add those entries here and re-run the collector.
"""

import logging
import os
import time
from datetime import datetime

import requests

from quant.collectors.base import BaseCollector
from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger('quant.collectors.event_calendar')

# ---------------------------------------------------------------------------
# FRED series mapping: event_type → (series_id, formatter_fn)
#
# Matcher: the fill logic compares event_type (exact match, case-insensitive).
# For CPI/Core-CPI/FOMC we also check the title for the "core" keyword.
#
# FRED series referenced here are already fetched nightly by fred_macro.py
# (they live in quant.macro) so we reuse the same series IDs and auth.
# ---------------------------------------------------------------------------

FRED_URL = 'https://api.stlouisfed.org/fred/series/observations'


def _pct(new_val, old_val, decimals=1):
    """Percent change from old_val to new_val."""
    if old_val and old_val != 0:
        return round((new_val - old_val) / old_val * 100, decimals)
    return None


def _fmt_pct(val, decimals=1):
    """Format a float percent value as e.g. '+0.2%' or '-0.1%'."""
    if val is None:
        return '?'
    sign = '+' if val >= 0 else ''
    return f"{sign}{val:.{decimals}f}%"


# Map event_type (uppercased) → config dict:
#   series_mom: FRED series for MoM/level data (latest obs)
#   series_yoy_period: how many periods back for YoY (12 = monthly)
#   formatter: callable(obs_list) -> formatted string  [obs_list newest→oldest]
#   check_title_for_core: if True, inspect title for 'core' keyword

def _fmt_cpi(obs):
    """CPI CPIAUCSL: format as '+X.X% MoM / Y.Y% YoY'."""
    if len(obs) < 2:
        return None
    mom = _pct(obs[0], obs[1])
    yoy = _pct(obs[0], obs[12]) if len(obs) > 12 else None
    parts = [f"CPI {_fmt_pct(mom)} MoM"]
    if yoy is not None:
        parts.append(f"{_fmt_pct(yoy)} YoY")
    return ' / '.join(parts)


def _fmt_core_cpi(obs):
    if len(obs) < 2:
        return None
    mom = _pct(obs[0], obs[1])
    yoy = _pct(obs[0], obs[12]) if len(obs) > 12 else None
    parts = [f"Core CPI {_fmt_pct(mom)} MoM"]
    if yoy is not None:
        parts.append(f"{_fmt_pct(yoy)} YoY")
    return ' / '.join(parts)


def _fmt_nfp(obs):
    """NFP PAYEMS: format as '+212K' (MoM change in thousands)."""
    if len(obs) < 2:
        return None
    # PAYEMS is in thousands of persons
    delta = obs[0] - obs[1]
    sign = '+' if delta >= 0 else ''
    return f"NFP {sign}{delta:.0f}K"


def _fmt_unemployment(obs):
    """UNRATE: level in percent."""
    if not obs:
        return None
    return f"Unemployment {obs[0]:.1f}%"


def _fmt_core_pce(obs):
    """PCEPILFE: MoM / YoY."""
    if len(obs) < 2:
        return None
    mom = _pct(obs[0], obs[1])
    yoy = _pct(obs[0], obs[12]) if len(obs) > 12 else None
    parts = [f"Core PCE {_fmt_pct(mom)} MoM"]
    if yoy is not None:
        parts.append(f"{_fmt_pct(yoy)} YoY")
    return ' / '.join(parts)


def _fmt_fomc(obs, prior_obs=None):
    """DFEDTARU: upper Fed funds target rate.

    Returns 'held at 3.75%' or 'cut/hiked 25bp to 3.50%'.
    obs[0] is current, prior_obs[0] is previous meeting value.
    """
    if not obs:
        return None
    current = obs[0]
    if prior_obs and len(prior_obs) >= 1:
        prior = prior_obs[0]
        delta_bp = round((current - prior) * 100)
        if delta_bp == 0:
            return f"Fed held at {current:.2f}%"
        direction = 'hiked' if delta_bp > 0 else 'cut'
        return f"Fed {direction} {abs(delta_bp)}bp to {current:.2f}%"
    return f"Fed funds upper target {current:.2f}%"


# Single config table: event_type → (series_id, formatter, needs_prior_series, prior_series_id)
# needs_prior_series: True for FOMC (we need the previous observation to compute delta)
EVENT_FRED_MAP = {
    'CPI':   ('CPIAUCSL',  _fmt_cpi,          False, None,        'core' in ''),
    'UNEMPLOYMENT': ('UNRATE', _fmt_unemployment, False, None,    False),
    'NFP':   ('PAYEMS',    _fmt_nfp,           False, None,        False),
    'FOMC':  ('DFEDTARU',  _fmt_fomc,          True,  'DFEDTARU',  False),
}
# Core CPI and Core PCE share the same event_type slots but we detect "core"
# in the title; they use different series
EVENT_FRED_MAP_CORE = {
    'CPI':   ('CPILFESL',  _fmt_core_cpi, False, None, True),
    'PCE':   ('PCEPILFE',  _fmt_core_pce, False, None, True),
}

# Plain PCE (non-core) — only if there is a PCE event type
EVENT_FRED_MAP_PCE = {
    'PCE':   ('PCEPILFE',  _fmt_core_pce, False, None, False),
}


def _fetch_fred_obs(series_id: str, api_key: str, n: int = 15) -> list[float]:
    """Fetch the most recent N observations of a FRED series.

    Returns a list of floats, newest first.  Empty list on any failure.
    Rate-limit tolerant: 0.3s sleep between calls in fill loop handles it.
    """
    try:
        resp = requests.get(FRED_URL, params={
            'series_id': series_id,
            'api_key': api_key,
            'file_type': 'json',
            'sort_order': 'desc',
            'limit': n,
        }, timeout=15)
        resp.raise_for_status()
        raw = resp.json().get('observations', [])
        vals = []
        for o in raw:
            v = o.get('value')
            if v not in (None, '.', ''):
                try:
                    vals.append(float(v))
                except (ValueError, TypeError):
                    pass
        return vals  # newest first (sort_order=desc)
    except Exception as e:
        safe = str(e).replace(api_key, '***') if api_key else str(e)
        logger.warning("[fill_actuals] FRED fetch %s failed: %s", series_id, safe)
        return []


def _obs_period_is_recent(series_id: str, api_key: str, release_date_str: str,
                           window_days: int = 60) -> bool:
    """Check that FRED's latest observation was released recently enough.

    For a release on 10 June we expect FRED to have published data whose
    observation date is within window_days before the release (e.g. CPI released
    in June covers May → obs date should be ≤ 60 days before the release date).
    Returns True if OK to use, False if data looks stale or period mismatches.
    """
    try:
        resp = requests.get(FRED_URL, params={
            'series_id': series_id,
            'api_key': api_key,
            'file_type': 'json',
            'sort_order': 'desc',
            'limit': 1,
        }, timeout=15)
        resp.raise_for_status()
        obs = resp.json().get('observations', [])
        if not obs:
            return False
        obs_date_str = obs[0].get('date', '')
        if not obs_date_str:
            return False
        obs_date = datetime.strptime(obs_date_str, '%Y-%m-%d').date()
        release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
        delta = (release_date - obs_date).days
        # obs_date must be in the past relative to release, and not too old
        return 0 <= delta <= window_days
    except Exception as e:
        safe = str(e).replace(api_key, '***') if api_key else str(e)
        logger.debug("[fill_actuals] period-check failed for %s: %s", series_id, safe)
        return False


def fill_event_actuals(date: str) -> int:
    """Fill actual values for events on `date` where FRED data is available.

    Args:
        date: ISO date string 'YYYY-MM-DD' — the release date (today).

    Returns the number of rows updated (0..N).

    Design:
    - Looks up events where ts = date AND actual IS NULL.
    - For each event_type in EVENT_FRED_MAP (or core variants detected by title):
        1. Verify FRED's latest obs is recent enough (period-check).
        2. Fetch the latest ~15 observations.
        3. Apply the formatter function.
        4. UPDATE quant.events SET actual = ..., actual_filled_at = NOW() WHERE id = ...
    - Tolerant: missing/late data leaves actual NULL, never crashes.
    - Called by the afternoon run AND the nightly FRED collector as backfill.
    """
    api_key = os.environ.get('FRED_API_KEY', '')
    if not api_key:
        logger.warning("[fill_actuals] FRED_API_KEY not set; skipping")
        return 0

    updated = 0
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, event_type, title
                FROM quant.events
                WHERE ts = %s AND actual IS NULL
            """, (date,))
            events = cur.fetchall()
    except Exception as e:
        logger.warning("[fill_actuals] DB fetch failed for date %s: %s", date, e)
        return 0

    if not events:
        logger.debug("[fill_actuals] No NULL-actual events for %s", date)
        return 0

    for evt in events:
        evt_id = evt['id']
        event_type = (evt['event_type'] or '').upper()
        title = (evt['title'] or '').lower()
        is_core = 'core' in title

        # Determine which FRED config to use
        fred_config = None
        if is_core and event_type in EVENT_FRED_MAP_CORE:
            fred_config = EVENT_FRED_MAP_CORE[event_type]
        elif event_type == 'PCE' and not is_core:
            fred_config = EVENT_FRED_MAP_PCE.get('PCE')
        elif event_type in EVENT_FRED_MAP:
            fred_config = EVENT_FRED_MAP[event_type]

        if fred_config is None:
            logger.debug("[fill_actuals] No FRED mapping for event_type=%s id=%d", event_type, evt_id)
            continue

        series_id, formatter, needs_prior, prior_series_id, _core_flag = fred_config

        # Period check: make sure FRED has data that matches this release window
        if not _obs_period_is_recent(series_id, api_key, date):
            logger.info("[fill_actuals] %s (%s): period-check failed, FRED data not yet available — leaving NULL",
                        event_type, series_id)
            time.sleep(0.3)
            continue

        time.sleep(0.3)  # FRED rate-limit politeness

        obs = _fetch_fred_obs(series_id, api_key, n=15)
        if not obs:
            logger.info("[fill_actuals] %s (%s): no obs returned — leaving NULL", event_type, series_id)
            continue

        # FOMC needs prior observation (compare current meeting vs previous)
        formatted = None
        try:
            if needs_prior and prior_series_id:
                time.sleep(0.3)
                prior_obs = _fetch_fred_obs(prior_series_id, api_key, n=2)
                # prior_obs[0]=current, prior_obs[1]=prev meeting value
                prior_only = prior_obs[1:2] if len(prior_obs) > 1 else []
                formatted = formatter(obs[:1], prior_only)
            else:
                formatted = formatter(obs)
        except Exception as e:
            logger.warning("[fill_actuals] formatter failed for %s id=%d: %s", event_type, evt_id, e)
            continue

        if not formatted:
            logger.debug("[fill_actuals] formatter returned None for %s id=%d", event_type, evt_id)
            continue

        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE quant.events
                    SET actual = %s, actual_filled_at = NOW()
                    WHERE id = %s AND actual IS NULL
                """, (formatted, evt_id))
                if cur.rowcount > 0:
                    updated += 1
                    logger.info("[fill_actuals] Filled %s id=%d: %s", event_type, evt_id, formatted)
        except Exception as e:
            logger.error("[fill_actuals] DB update failed for id=%d: %s", evt_id, e)

    logger.info("[fill_actuals] date=%s: %d / %d events filled", date, updated, len(events))
    return updated

# Hardcoded schedule for 2026 and 2027.
# Sources: federalreserve.gov, bls.gov, ecb.europa.eu, boj.or.jp
# NOTE: CPI, NFP, and BoJ 2027 dates are NOT yet published.
#       Add them here when BLS (~Oct 2026) and BoJ publish their 2027 schedules.
SCHEDULED_EVENTS = [
    # FOMC rate decisions (2026) — 8 meetings
    {'ts': '2026-01-28', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision', 'impact': 'high'},
    {'ts': '2026-03-18', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision + SEP/Dot Plot', 'impact': 'high'},
    {'ts': '2026-05-06', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision', 'impact': 'high'},
    {'ts': '2026-06-17', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision + SEP/Dot Plot', 'impact': 'high'},
    {'ts': '2026-07-29', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision', 'impact': 'high'},
    {'ts': '2026-09-16', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision + SEP/Dot Plot', 'impact': 'high'},
    {'ts': '2026-11-04', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision', 'impact': 'high'},
    {'ts': '2026-12-16', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision + SEP/Dot Plot', 'impact': 'high'},

    # CPI releases (2026) — ~12th-14th of each month, reporting prior month
    {'ts': '2026-01-14', 'event_type': 'CPI', 'title': 'CPI Release (Dec 2025)', 'impact': 'high'},
    {'ts': '2026-02-11', 'event_type': 'CPI', 'title': 'CPI Release (Jan)', 'impact': 'high'},
    {'ts': '2026-03-11', 'event_type': 'CPI', 'title': 'CPI Release (Feb)', 'impact': 'high'},
    {'ts': '2026-04-14', 'event_type': 'CPI', 'title': 'CPI Release (Mar)', 'impact': 'high'},
    {'ts': '2026-05-12', 'event_type': 'CPI', 'title': 'CPI Release (Apr)', 'impact': 'high'},
    {'ts': '2026-06-10', 'event_type': 'CPI', 'title': 'CPI Release (May)', 'impact': 'high'},
    {'ts': '2026-07-14', 'event_type': 'CPI', 'title': 'CPI Release (Jun)', 'impact': 'high'},
    {'ts': '2026-08-12', 'event_type': 'CPI', 'title': 'CPI Release (Jul)', 'impact': 'high'},
    {'ts': '2026-09-11', 'event_type': 'CPI', 'title': 'CPI Release (Aug)', 'impact': 'high'},
    {'ts': '2026-10-13', 'event_type': 'CPI', 'title': 'CPI Release (Sep)', 'impact': 'high'},
    {'ts': '2026-11-12', 'event_type': 'CPI', 'title': 'CPI Release (Oct)', 'impact': 'high'},
    {'ts': '2026-12-10', 'event_type': 'CPI', 'title': 'CPI Release (Nov)', 'impact': 'high'},

    # NFP — Non-Farm Payrolls (first Friday of month)
    {'ts': '2026-01-09', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Dec 2025)', 'impact': 'high'},
    {'ts': '2026-02-06', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Jan)', 'impact': 'high'},
    {'ts': '2026-03-06', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Feb)', 'impact': 'high'},
    {'ts': '2026-04-03', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Mar)', 'impact': 'high'},
    {'ts': '2026-05-08', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Apr)', 'impact': 'high'},
    {'ts': '2026-06-05', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (May)', 'impact': 'high'},
    {'ts': '2026-07-02', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Jun)', 'impact': 'high'},
    {'ts': '2026-08-07', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Jul)', 'impact': 'high'},
    {'ts': '2026-09-04', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Aug)', 'impact': 'high'},
    {'ts': '2026-10-02', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Sep)', 'impact': 'high'},
    {'ts': '2026-11-06', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Oct)', 'impact': 'high'},
    {'ts': '2026-12-04', 'event_type': 'NFP', 'title': 'Non-Farm Payrolls (Nov)', 'impact': 'high'},

    # ECB rate decisions (2026) — 6 meetings
    {'ts': '2026-01-22', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2026-03-05', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2026-04-16', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2026-06-04', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2026-07-16', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2026-09-10', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2026-10-22', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2026-12-17', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},

    # BoJ rate decisions (2026) — key meetings
    {'ts': '2026-01-24', 'event_type': 'BOJ', 'title': 'BoJ Rate Decision', 'impact': 'medium'},
    {'ts': '2026-03-13', 'event_type': 'BOJ', 'title': 'BoJ Rate Decision + Outlook', 'impact': 'medium'},
    {'ts': '2026-04-28', 'event_type': 'BOJ', 'title': 'BoJ Rate Decision + Outlook', 'impact': 'medium'},
    {'ts': '2026-06-16', 'event_type': 'BOJ', 'title': 'BoJ Rate Decision', 'impact': 'medium'},
    {'ts': '2026-07-15', 'event_type': 'BOJ', 'title': 'BoJ Rate Decision + Outlook', 'impact': 'medium'},
    {'ts': '2026-09-17', 'event_type': 'BOJ', 'title': 'BoJ Rate Decision', 'impact': 'medium'},
    {'ts': '2026-10-29', 'event_type': 'BOJ', 'title': 'BoJ Rate Decision + Outlook', 'impact': 'medium'},
    {'ts': '2026-12-18', 'event_type': 'BOJ', 'title': 'BoJ Rate Decision', 'impact': 'medium'},

    # Quarterly options expiry (Triple Witching)
    {'ts': '2026-03-20', 'event_type': 'OPTIONS_EXPIRY', 'title': 'Q1 Options Expiry (Triple Witching)', 'impact': 'medium'},
    {'ts': '2026-06-19', 'event_type': 'OPTIONS_EXPIRY', 'title': 'Q2 Options Expiry (Triple Witching)', 'impact': 'medium'},
    {'ts': '2026-09-18', 'event_type': 'OPTIONS_EXPIRY', 'title': 'Q3 Options Expiry (Triple Witching)', 'impact': 'medium'},
    {'ts': '2026-12-18', 'event_type': 'OPTIONS_EXPIRY', 'title': 'Q4 Options Expiry (Triple Witching)', 'impact': 'medium'},

    # ── 2027 ─────────────────────────────────────────────────────────
    # FOMC rate decisions (2027) — 8 meetings
    # Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (confirmed)
    # Dates are tentative until confirmed at the preceding meeting.
    {'ts': '2027-01-27', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision', 'impact': 'high'},
    {'ts': '2027-03-17', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision + SEP/Dot Plot', 'impact': 'high'},
    {'ts': '2027-04-28', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision', 'impact': 'high'},
    {'ts': '2027-06-09', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision + SEP/Dot Plot', 'impact': 'high'},
    {'ts': '2027-07-28', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision', 'impact': 'high'},
    {'ts': '2027-09-15', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision + SEP/Dot Plot', 'impact': 'high'},
    {'ts': '2027-10-27', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision', 'impact': 'high'},
    {'ts': '2027-12-08', 'event_type': 'FOMC', 'title': 'FOMC Rate Decision + SEP/Dot Plot', 'impact': 'high'},

    # ECB rate decisions (2027) — 7 meetings
    # Source: https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html (confirmed)
    # Dates are press-conference day (Day 2 of each two-day meeting).
    {'ts': '2027-02-04', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2027-03-18', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2027-06-10', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2027-07-22', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2027-09-09', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2027-10-28', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},
    {'ts': '2027-12-16', 'event_type': 'ECB', 'title': 'ECB Rate Decision', 'impact': 'medium'},

    # CPI releases (2027) — NOT YET PUBLISHED by BLS
    # BLS publishes the following year's schedule ~October. Add when available:
    # https://www.bls.gov/schedule/news_release/cpi.htm

    # NFP — Non-Farm Payrolls (2027) — NOT YET PUBLISHED by BLS
    # BLS publishes the following year's schedule ~October. Add when available:
    # https://www.bls.gov/schedule/news_release/empsit.htm

    # BoJ rate decisions (2027) — NOT YET PUBLISHED by BoJ
    # BoJ has not yet released 2027 MPM dates. Add when available:
    # https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm

    # Quarterly options expiry (Triple Witching) 2027
    # Third Friday of March, June, September, December
    {'ts': '2027-03-19', 'event_type': 'OPTIONS_EXPIRY', 'title': 'Q1 Options Expiry (Triple Witching)', 'impact': 'medium'},
    {'ts': '2027-06-18', 'event_type': 'OPTIONS_EXPIRY', 'title': 'Q2 Options Expiry (Triple Witching)', 'impact': 'medium'},
    {'ts': '2027-09-17', 'event_type': 'OPTIONS_EXPIRY', 'title': 'Q3 Options Expiry (Triple Witching)', 'impact': 'medium'},
    {'ts': '2027-12-17', 'event_type': 'OPTIONS_EXPIRY', 'title': 'Q4 Options Expiry (Triple Witching)', 'impact': 'medium'},
]


class EventCalendarCollector(BaseCollector):
    name = 'event_calendar'

    def collect(self):
        logger.info("[event_calendar] Inserting %d scheduled events", len(SCHEDULED_EVENTS))

        if self.dry_run:
            logger.info("[event_calendar] DRY RUN: would insert %d events", len(SCHEDULED_EVENTS))
            return

        inserted = 0
        with get_conn() as conn:
            cur = conn.cursor()
            for evt in SCHEDULED_EVENTS:
                cur.execute("""
                    INSERT INTO quant.events (ts, event_type, title, description, impact)
                    SELECT %s, %s, %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM quant.events
                        WHERE ts = %s AND event_type = %s AND title = %s
                    )
                """, (
                    evt['ts'], evt['event_type'], evt['title'],
                    evt.get('description'), evt.get('impact'),
                    evt['ts'], evt['event_type'], evt['title'],
                ))
                if cur.rowcount > 0:
                    inserted += 1

        self._rows_upserted = inserted
        logger.info(f"  Inserted {inserted} new events ({len(SCHEDULED_EVENTS) - inserted} already existed)")


log = logging.getLogger(__name__)


# ── Forward consensus: deliberately NOT fetched ──────────────────────────────
#
# Finnhub's /calendar/economic is a PREMIUM feature: on the plan in use it
# answers HTTP 403 ("You don't have access to this resource"). Calling it as a
# graceful no-op "in case the plan is upgraded" has no purpose when the plan
# will not be upgraded, and a 403 on a query-parameter credential risks
# leaking the API key into the engine log — the safest request is the one not
# made.
#
# ⚠ THE RULE THIS ENFORCES MATTERS MORE THAN THE FETCHER:
# `quant.events.expected` is NEVER populated, no free source for forward
# consensus exists, and REPORT PROMPTS MUST NOT INVENT ONE. The Docket carries
# no consensus/expected column for this reason — see carrac/facts/bands.py.
# Forward event DATES are real and current, from fetch_fred_release_events()
# below; actuals come from fill_event_actuals().


# ---------------------------------------------------------------------------
# FRED release-date calendar (fetch_fred_release_events)
#
# Forward dates for the major US macro prints that are NOT in SCHEDULED_EVENTS
# (PCE, PPI, GDP) come from FRED's free release-dates API — the same FRED auth
# used for actuals. Real, auto-updating dates only — never hardcoded/guessed.
# Complements the hardcoded central-bank + CPI/NFP schedule; dedups on
# (ts, event_type) so re-runs are idempotent. Actuals for these are backfilled
# post-release by fill_event_actuals() where a FRED formatter exists.
#
# fetch_fred_release_events() also runs a PAST-window
# backfill for the same PCE/PPI/GDP release_ids (see _backfill_past_release_
# events below) so carrac.facts.event_study's n>=8 gate can clear on real FRED
# history instead of staying starved forever. The backfill piggybacks on this
# function's existing call sites (quant.collectors.fred_macro's nightly
# collect(), morning_engine's afternoon refresh) — no new cron entry.
#
# SCHEDULED_EVENTS (FOMC/CPI/NFP/ECB/BOJ/OPTIONS_EXPIRY) is a hand-maintained
# literal list, not a generator — there is no programmatic way to derive past
# dates from it, and this repo's own bar for a date in this file is a cited,
# human-verified primary source (see the module docstring's "2027 source
# verification" block). FRED tracks CPI (Consumer Price Index) and the
# Employment Situation (NFP) as numbered releases, so those two are
# backfilled the same way as PCE/PPI/GDP via
# FRED_RELEASE_EVENTS_BACKFILL_ONLY below. A release_id must be
# live-verified against the FRED release API before it is added: a guessed
# id would silently mislabel a different release's dates as "CPI"/"NFP"
# history, which is exactly the kind of invented data this module forbids.
# release_id 10 returns "Consumer Price Index" and release_id 50 returns
# "Employment Situation". FOMC/ECB/BOJ decisions aren't FRED "releases" at
# all; their history would need a hand-curated, cited SCHEDULED_EVENTS_PAST
# list sourced the way SCHEDULED_EVENTS itself is.
# ---------------------------------------------------------------------------

FRED_RELEASE_URL = 'https://api.stlouisfed.org/fred/release/dates'

# release_id → (event_type, title, impact). Only releases absent from
# SCHEDULED_EVENTS (which already covers FOMC/CPI/NFP/ECB/BOJ/expiry).
FRED_RELEASE_EVENTS = {
    54: ('PCE', 'Personal Income & Outlays (PCE)', 'high'),    # Personal Income and Outlays
    46: ('PPI', 'Producer Price Index (PPI)',      'medium'),  # Producer Price Index
    53: ('GDP', 'GDP Release',                     'medium'),  # Gross Domestic Product
}

# BACKFILL-ONLY releases: forward dates for these come from SCHEDULED_EVENTS
# (hand-maintained), so they must NOT join the forward fetch — but their
# HISTORY is real FRED release data. event_type strings match SCHEDULED_EVENTS
# ('CPI'/'NFP') so the (ts, event_type) dedup unifies both sources.
FRED_RELEASE_EVENTS_BACKFILL_ONLY = {
    10: ('CPI', 'CPI Release',       'high'),  # "Consumer Price Index" (live-verified)
    50: ('NFP', 'Non-Farm Payrolls', 'high'),  # "Employment Situation" (live-verified)
}

# Backfill tuning. Mirrors carrac.facts.event_study.event_next_day_stats'
# defaults (min_n=8, lookback_days=730) so the backfilled history is exactly
# deep enough to clear that module's n-gate — no import of carrac from quant
# (kept a one-way dependency), just the same numbers by convention.
_BACKFILL_MIN_PAST_ROWS = 8
_BACKFILL_YEARS = 2


def _fetch_fred_release_dates(release_id: int, api_key: str, lo: str, hi: str) -> list:
    """Fetch FRED release dates for `release_id`, filtered to [lo, hi]
    (inclusive ISO date strings). Returns [] on any failure — shared by the
    forward fetch and the historical backfill so both stay in lockstep."""
    try:
        resp = requests.get(FRED_RELEASE_URL, params={
            'release_id': release_id, 'api_key': api_key, 'file_type': 'json',
            'sort_order': 'asc', 'include_release_dates_with_no_data': 'true',
            'realtime_start': lo,
        }, timeout=15)
        resp.raise_for_status()
        dates = [d.get('date') for d in resp.json().get('release_dates', [])]
    except Exception as e:
        safe = str(e).replace(api_key, '***') if api_key else str(e)
        logger.warning("[fred_release_events] release %s fetch failed: %s", release_id, safe)
        return []
    return [d for d in dates if d and lo <= d <= hi]


def _upsert_release_events(rows: list) -> int:
    """Idempotent upsert of (ts, event_type, title, impact) tuples into
    quant.events, deduped on (ts, event_type). Returns rows actually
    inserted (0 for already-existing rows). Shared by the forward fetch and
    the historical backfill."""
    if not rows:
        return 0
    inserted = 0
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            for ts, etype, title, impact in rows:
                cur.execute("""
                    INSERT INTO quant.events (ts, event_type, title, impact)
                    SELECT %s, %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM quant.events WHERE ts = %s AND event_type = %s
                    )
                """, (ts, etype, title, impact, ts, etype))
                if cur.rowcount > 0:
                    inserted += 1
            conn.commit()
    except Exception as e:
        logger.error("[fred_release_events] DB upsert failed (%d rows): %s", len(rows), e)
        return 0
    return inserted


def _count_past_events(event_type: str, before: str) -> int:
    """COUNT of existing quant.events rows for `event_type` strictly before
    `before` (ISO date string). Returns 0 on any failure (treated as
    "not backfilled yet", so a transient DB hiccup just retries the backfill
    next run rather than silently skipping it forever)."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM quant.events WHERE event_type = %s AND ts < %s",
                (event_type, before),
            )
            row = cur.fetchone()
            return int(row['cnt']) if row else 0
    except Exception as e:
        logger.debug("[fred_release_events] backfill count failed for %s: %s", event_type, e)
        return 0


def _backfill_past_release_events(api_key: str, years_back: int = _BACKFILL_YEARS,
                                   today=None) -> int:
    """Historical backfill of PCE/PPI/GDP release dates from FRED (real
    government release history — never guessed), so carrac.facts.
    event_study's n>=8 gate can clear.

    Cheap on every nightly re-run BY DESIGN: for each event_type, a single
    COUNT query checks whether it already has >= _BACKFILL_MIN_PAST_ROWS past
    rows; only when sparse does this issue a FRED network call for that type.
    Once a type clears the gate, subsequent runs are one COUNT query per
    type — zero network calls, zero inserts (idempotent dedup would no-op
    anyway, but we skip the round trip for cost). Never raises; any failure
    here just means the type stays sparse and gets retried next run.

    `today`: optional override (a date) for tests; defaults to real today.
    """
    from datetime import date as _date, timedelta as _td
    today = today or _date.today()
    lo = (today - _td(days=365 * years_back)).isoformat()
    hi = (today - _td(days=1)).isoformat()  # yesterday — forward fetch owns today+

    to_insert = []  # (ts, event_type, title, impact)
    for rid, (etype, title, impact) in {**FRED_RELEASE_EVENTS, **FRED_RELEASE_EVENTS_BACKFILL_ONLY}.items():
        existing = _count_past_events(etype, today.isoformat())
        if existing >= _BACKFILL_MIN_PAST_ROWS:
            logger.debug("[fred_release_events] backfill skip %s (%d past rows already)",
                         etype, existing)
            continue
        dates = _fetch_fred_release_dates(rid, api_key, lo, hi)
        to_insert.extend((d, etype, title, impact) for d in dates)

    inserted = _upsert_release_events(to_insert)
    if inserted:
        logger.info("[fred_release_events] backfill inserted %d historical events (%dy back)",
                    inserted, years_back)
    return inserted


def fetch_fred_release_events(window_days: int = 45) -> int:
    """Upsert upcoming US macro release dates (PCE/PPI/GDP) from FRED, plus a
    cheap self-limiting historical backfill of the same types (see
    _backfill_past_release_events).

    Free + auto-updating; fills the gaps between the hardcoded central-bank and
    CPI/NFP events. Real scheduled dates only. Idempotent (dedups on ts+type).
    Returns the number of NEW rows inserted (forward + backfill combined).
    """
    api_key = os.environ.get('FRED_API_KEY', '')
    if not api_key:
        logger.warning("[fred_release_events] FRED_API_KEY not set; skipping")
        return 0

    from datetime import date as _date, timedelta as _td
    today = _date.today()
    lo, hi = today.isoformat(), (today + _td(days=window_days)).isoformat()

    to_insert = []  # (ts, event_type, title, impact)
    for rid, (etype, title, impact) in FRED_RELEASE_EVENTS.items():
        dates = _fetch_fred_release_dates(rid, api_key, lo, hi)
        to_insert.extend((d, etype, title, impact) for d in dates)

    if not to_insert:
        logger.info("[fred_release_events] no upcoming releases in next %dd", window_days)
        inserted = 0
    else:
        inserted = _upsert_release_events(to_insert)
        logger.info("[fred_release_events] inserted %d new events (window %dd)", inserted, window_days)

    # Backfill is best-effort and independent of the forward fetch outcome —
    # a forward-fetch failure (e.g. transient FRED hiccup) should never block
    # the historical backfill from having its own shot.
    try:
        inserted += _backfill_past_release_events(api_key)
    except Exception as e:
        logger.debug("[fred_release_events] backfill skipped (non-fatal): %s", e)

    return inserted


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('Macro event calendar collector')
    collector = EventCalendarCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
    )
    collector.run()
