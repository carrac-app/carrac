"""
Market Sessions indicator — server-side session classification.

Classifies each candle timestamp into trading sessions (Asia, UK, US) with
sub-phases (gap, session, extension/reversal) and detects market holidays.

Session times are defined in local time for each financial center:
- Asia: Fixed UTC (Tokyo/HK don't observe DST)
- UK: London local time (shifts with BST)
- US: New York local time (shifts with EDT)
"""

import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger('carrac.charts.sessions')

# ---------------------------------------------------------------------------
# Session time definitions (in local time for each market)
# ---------------------------------------------------------------------------

# Asia — fixed UTC (no DST in Tokyo/HK)
ASIA_GAP_START = 0       # 00:00 UTC
ASIA_SESSION_START = 1   # 01:00 UTC
ASIA_EXTENSION_START = 5 # 05:00 UTC
ASIA_END = 7             # 07:00 UTC

# UK — London local time (GMT/BST)
UK_GAP_START = 7         # 07:00 London (pre-open)
UK_SESSION_START = 8     # 08:00 London (LSE open)
UK_EXTENSION_START = 12  # 12:00 London (afternoon)
UK_END = 13              # 13:00 London

# US — New York local time (EST/EDT)
US_GAP_START = 7         # 07:00 New York (pre-market)
US_SESSION_START = 9.5   # 09:30 New York (NYSE open)
US_REVERSAL_START = 12   # 12:00 New York (PM session)
US_END = 16              # 16:00 New York (NYSE close)

TZ_LONDON = ZoneInfo('Europe/London')
TZ_NEW_YORK = ZoneInfo('America/New_York')

# ---------------------------------------------------------------------------
# Holiday calendar (major market holidays — crypto still trades but low liq)
# ---------------------------------------------------------------------------

# Fixed-date holidays: (month, day) → (name, affected_markets)
# Markets: 'all', 'us', 'uk', 'asia', or comma-separated like 'us,uk'
FIXED_HOLIDAYS = {
    (1, 1):   ('New Year\'s Day', 'all'),
    (6, 19):  ('Juneteenth', 'us'),
    (7, 4):   ('Independence Day', 'us'),
    (12, 25): ('Christmas Day', 'all'),
    (12, 26): ('Boxing Day', 'uk'),
}


def _easter_date(year):
    """Compute Easter Sunday using the Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime(year, month, day)


def _get_holidays_for_year(year):
    """Build holiday set for a given year: (month, day) → description."""
    holidays = dict(FIXED_HOLIDAYS)

    # Easter-based holidays (global market impact)
    easter = _easter_date(year)
    good_friday = easter - timedelta(days=2)
    easter_monday = easter + timedelta(days=1)
    holidays[(good_friday.month, good_friday.day)] = ('Good Friday', 'all')
    holidays[(easter_monday.month, easter_monday.day)] = ('Easter Monday', 'uk')

    # --- Asia holidays (set first — UK/US names take precedence on conflicts) ---

    # Japanese market holidays (TSE closed)
    holidays[(1, 2)] = ('Japan Bank Holiday', 'asia')
    holidays[(1, 3)] = ('Japan Bank Holiday', 'asia')
    holidays[(2, 11)] = ('Japan National Day', 'asia')
    holidays[(2, 23)] = ('Emperor\'s Birthday', 'asia')
    holidays[(3, 20)] = ('Vernal Equinox', 'asia')
    holidays[(4, 29)] = ('Showa Day', 'asia')
    holidays[(5, 3)] = ('Constitution Day', 'asia')
    holidays[(5, 4)] = ('Greenery Day', 'asia')
    holidays[(5, 5)] = ('Children\'s Day', 'asia')
    holidays[(11, 3)] = ('Culture Day', 'asia')
    holidays[(11, 23)] = ('Japan Labor Day', 'asia')

    # Chinese New Year (HK/China markets closed ~3 days)
    _cny = {
        2025: (1, 29), 2026: (2, 17), 2027: (2, 6), 2028: (1, 26),
        2029: (2, 13), 2030: (2, 3),
    }
    if year in _cny:
        m, d = _cny[year]
        for offset in range(3):
            dt = datetime(year, m, d) + timedelta(days=offset)
            holidays[(dt.month, dt.day)] = ('Chinese New Year', 'asia')

    # --- UK floating holidays ---

    may31 = datetime(year, 5, 31)

    may1 = datetime(year, 5, 1)
    days_to_mon = (0 - may1.weekday()) % 7
    if days_to_mon == 0 and may1.weekday() != 0:
        days_to_mon = 7
    uk_may = may1 + timedelta(days=days_to_mon)
    holidays[(uk_may.month, uk_may.day)] = ('UK Early May', 'uk')

    uk_spring = may31 - timedelta(days=(may31.weekday() - 0) % 7)
    holidays[(uk_spring.month, uk_spring.day)] = ('UK Spring Bank', 'uk')

    aug31 = datetime(year, 8, 31)
    days_back_aug = (aug31.weekday() - 0) % 7
    uk_summer = aug31 - timedelta(days=days_back_aug)
    holidays[(uk_summer.month, uk_summer.day)] = ('UK Summer Bank', 'uk')

    # --- US floating holidays (set last — most relevant for crypto traders) ---

    jan1 = datetime(year, 1, 1)
    days_to_mon = (0 - jan1.weekday()) % 7
    if days_to_mon == 0 and jan1.weekday() != 0:
        days_to_mon = 7
    mlk = jan1 + timedelta(days=days_to_mon + 14)
    holidays[(mlk.month, mlk.day)] = ('MLK Day', 'us')

    feb1 = datetime(year, 2, 1)
    days_to_mon = (0 - feb1.weekday()) % 7
    if days_to_mon == 0 and feb1.weekday() != 0:
        days_to_mon = 7
    presidents = feb1 + timedelta(days=days_to_mon + 14)
    holidays[(presidents.month, presidents.day)] = ('Presidents\' Day', 'us')

    memorial = may31 - timedelta(days=(may31.weekday() - 0) % 7)
    holidays[(memorial.month, memorial.day)] = ('Memorial Day', 'us')

    sep1 = datetime(year, 9, 1)
    days_to_mon = (0 - sep1.weekday()) % 7
    if days_to_mon == 0 and sep1.weekday() != 0:
        days_to_mon = 7
    labor = sep1 + timedelta(days=days_to_mon)
    holidays[(labor.month, labor.day)] = ('Labor Day', 'us')

    nov1 = datetime(year, 11, 1)
    days_until_thu = (3 - nov1.weekday()) % 7
    thanksgiving = nov1 + timedelta(days=days_until_thu + 21)
    holidays[(thanksgiving.month, thanksgiving.day)] = ('Thanksgiving', 'us')

    return holidays


# Cache holidays per year
_holiday_cache = {}


def get_holiday(dt_utc):
    """Check if a UTC datetime falls on a market holiday.
    Returns (name, markets) tuple or None.
    markets is 'all', 'us', 'uk', 'asia', etc."""
    year = dt_utc.year
    if year not in _holiday_cache:
        _holiday_cache[year] = _get_holidays_for_year(year)
    holidays = _holiday_cache[year]
    key = (dt_utc.month, dt_utc.day)
    return holidays.get(key)


def is_weekend(dt_utc):
    """Check if UTC datetime is on a weekend (Sat=5, Sun=6)."""
    return dt_utc.weekday() >= 5


# ---------------------------------------------------------------------------
# Session classification
# ---------------------------------------------------------------------------

def _utc_hour_for_local(dt_utc, tz, local_hour):
    """Convert a local hour to UTC hour for a given date, accounting for DST."""
    local_dt = dt_utc.astimezone(tz)
    local_target = local_dt.replace(hour=local_hour, minute=0, second=0, microsecond=0)
    utc_target = local_target.astimezone(timezone.utc)
    return utc_target.hour + (utc_target.day - dt_utc.day) * 24


def classify_timestamp(ts_epoch):
    """Classify a Unix timestamp into session/phase.

    Returns dict:
        {
            'session': 'asia'|'uk'|'us'|'dead',
            'phase': 'gap'|'session'|'extension'|'reversal'|'dead',
            'holiday': str|None  — holiday name if this session is affected
            'holidayMarkets': str|None — which markets: 'all','us','uk','asia'
        }
    """
    dt = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
    utc_hour = dt.hour + dt.minute / 60.0

    # Holiday check — returns (name, markets) or None
    holiday_info = get_holiday(dt)
    holiday_name = holiday_info[0] if holiday_info else None
    holiday_markets = holiday_info[1] if holiday_info else None
    if is_weekend(dt):
        holiday_name = holiday_name or 'Weekend'
        holiday_markets = 'all'

    # Convert UK/US session boundaries to UTC for this specific date
    london_dt = dt.astimezone(TZ_LONDON)
    ny_dt = dt.astimezone(TZ_NEW_YORK)

    # London offset: hours ahead of UTC (0 in winter, 1 in summer)
    london_offset = london_dt.utcoffset().total_seconds() / 3600
    # NY offset: hours behind UTC (-5 in winter, -4 in summer)
    ny_offset = ny_dt.utcoffset().total_seconds() / 3600

    # UK times in UTC
    uk_gap_utc = UK_GAP_START - london_offset
    uk_session_utc = UK_SESSION_START - london_offset
    uk_ext_utc = UK_EXTENSION_START - london_offset
    uk_end_utc = UK_END - london_offset

    # US times in UTC
    us_gap_utc = US_GAP_START - ny_offset
    us_session_utc = US_SESSION_START - ny_offset
    us_reversal_utc = US_REVERSAL_START - ny_offset
    us_end_utc = US_END - ny_offset

    def _result(sess, phase):
        """Build result dict, only attaching holiday if this session is affected."""
        if holiday_name and holiday_markets:
            affected = holiday_markets == 'all' or sess in holiday_markets.split(',')
            if affected:
                return {'session': sess, 'phase': phase, 'holiday': holiday_name}
        return {'session': sess, 'phase': phase}

    # Asia (fixed UTC)
    if ASIA_GAP_START <= utc_hour < ASIA_SESSION_START:
        return _result('asia', 'gap')
    if ASIA_SESSION_START <= utc_hour < ASIA_EXTENSION_START:
        return _result('asia', 'session')
    if ASIA_EXTENSION_START <= utc_hour < ASIA_END:
        return _result('asia', 'extension')

    # UK (DST-aware)
    if uk_gap_utc <= utc_hour < uk_session_utc:
        return _result('uk', 'gap')
    if uk_session_utc <= utc_hour < uk_ext_utc:
        return _result('uk', 'session')
    if uk_ext_utc <= utc_hour < uk_end_utc:
        return _result('uk', 'extension')

    # US (DST-aware)
    if us_gap_utc <= utc_hour < us_session_utc:
        return _result('us', 'gap')
    if us_session_utc <= utc_hour < us_reversal_utc:
        return _result('us', 'session')
    if us_reversal_utc <= utc_hour < us_end_utc:
        return _result('us', 'reversal')

    # Dead zone (between US close and Asia open)
    return _result('dead', 'dead')


# ---------------------------------------------------------------------------
# Candle session data for chart API
# ---------------------------------------------------------------------------

# Timeframes where sessions are visible (4H and below)
SESSION_VISIBLE_TFS = frozenset(['1m', '3m', '5m', '15m', '30m', '1h', '4h', '4H'])


def compute_session_data(ts_list, timeframe):
    """Compute session classification for a list of candle timestamps.

    Args:
        ts_list: List of Unix epoch timestamps (seconds).
        timeframe: Timeframe string (e.g., '15m', '1h', '4h', '1D').

    Returns:
        List of session dicts if timeframe <= 4H, else empty list.
        Each dict: {session, phase, holiday, label}
        'label' is set only on the first candle of each new session.
    """
    if timeframe not in SESSION_VISIBLE_TFS:
        return []

    sessions = []
    prev_session = None

    for ts in ts_list:
        info = classify_timestamp(ts)

        # Set label on first candle of a new session
        cur_session = info['session']
        if cur_session != prev_session and cur_session != 'dead':
            info['label'] = cur_session.upper() if cur_session != 'dead' else None
        prev_session = cur_session

        sessions.append(info)

    return sessions


def compute_future_sessions(last_ts, timeframe, count=50):
    """Generate session data for future (unformed) candles.

    Args:
        last_ts: Unix epoch of the last real candle.
        timeframe: Timeframe string.
        count: Number of future candles to project.

    Returns:
        List of dicts: [{time, session, phase, holiday, label}, ...]
    """
    if timeframe not in SESSION_VISIBLE_TFS:
        return []

    # Timeframe → seconds per candle
    tf_seconds = {
        '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
        '1h': 3600, '4h': 14400, '4H': 14400
    }
    interval = tf_seconds.get(timeframe, 3600)

    future = []
    prev_session = None

    for i in range(1, count + 1):
        ts = last_ts + i * interval
        info = classify_timestamp(ts)
        info['time'] = ts

        cur_session = info['session']
        if cur_session != prev_session and cur_session != 'dead':
            info['label'] = cur_session.upper()
        prev_session = cur_session

        future.append(info)

    return future
