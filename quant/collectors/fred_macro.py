"""
FRED macro data collector for Quant DB.

Fetches ~23 economic time series from the Federal Reserve (FRED API).
Backfill default: 2000-01-01 to present.
Daily mode: fetches a trailing window per series (30 days for 'daily'-tagged
series, 90 days otherwise) — the wider window for weekly/monthly series is
required because a series' PUBLICATION cadence can be coarser than its
observation cadence (see DAILY_WINDOW_DAYS below).

Usage:
    python -m quant.collectors.fred_macro
    python -m quant.collectors.fred_macro --backfill
    python -m quant.collectors.fred_macro --backfill --backfill-start 1990-01-01
    python -m quant.collectors.fred_macro --dry-run
"""

import logging
import os
import time
import requests
from datetime import datetime, timedelta, timezone

from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.fred_macro')

# All FRED series: metric_key -> (series_id, frequency)
# frequency is informational only (FRED returns whatever is available)
FRED_SERIES = {
    # Daily rates & yields
    'FED_FUNDS':        ('DFF',             'daily'),
    'YIELD_2Y':         ('DGS2',            'daily'),
    'YIELD_10Y':        ('DGS10',           'daily'),
    'YIELD_10Y2Y':      ('T10Y2Y',          'daily'),
    'YIELD_10Y3M':      ('T10Y3M',          'daily'),      # 10Y-3M spread (recession signal)
    'BREAKEVEN_10Y':    ('T10YIE',          'daily'),
    'DXY':              ('DTWEXBGS',         'daily'),
    'VIX':              ('VIXCLS',           'daily'),
    'HY_SPREAD':        ('BAMLH0A0HYM2',    'daily'),
    'HY_SPREAD_EU':     ('BAMLHE00EHYIEY',  'daily'),      # EU high-yield spread
    'POLICY_UNCERT':    ('USEPUINDXD',      'daily'),      # Economic Policy Uncertainty Index
    # Daily prices
    'SP500':            ('SP500',            'daily'),
    'OIL_WTI':          ('DCOILWTICO',       'daily'),
    # Daily FX
    'FX_EURUSD':        ('DEXUSEU',          'daily'),
    'FX_USDJPY':        ('DEXJPUS',          'daily'),
    'FX_USDCNY':        ('DEXCHUS',          'daily'),
    # Weekly
    'M2':               ('WM2NS',            'weekly'),
    'FED_BALANCE':      ('WALCL',            'weekly'),
    'TGA':              ('WTREGEN',          'weekly'),     # Treasury General Account
    'REVERSE_REPO':     ('RRPONTSYD',       'daily'),      # Reverse Repo (for net liquidity calc)
    'INITIAL_CLAIMS':   ('ICSA',             'weekly'),     # Initial jobless claims
    # Monthly
    'M1':               ('M1SL',             'monthly'),
    'CPI':              ('CPIAUCSL',         'monthly'),
    'PPI':              ('PPIACO',           'monthly'),    # Producer Price Index
    'UNEMPLOYMENT':     ('UNRATE',           'monthly'),
    'CONSUMER_SENT':    ('UMCSENT',          'monthly'),
    'HOUSING_PERMITS':  ('PERMIT',           'monthly'),
    'HOUSING_STARTS':   ('HOUST',            'monthly'),
    'RETAIL_SALES':     ('RSAFS',            'monthly'),
    'INDPRO':           ('INDPRO',           'monthly'),
    'DURABLE_GOODS':    ('DGORDER',          'monthly'),
    'FED_FUNDS_EFF':    ('FEDFUNDS',         'monthly'),
    # Monthly - Global
    'ECB_RATE':         ('ECBDFR',           'monthly'),   # ECB deposit facility rate
    'JAPAN_10Y':        ('IRLTLT01JPM156N',  'monthly'),   # Japan 10Y gov bond yield
    'PMI_US':           ('BSCICP03USM665S',  'monthly'),   # US ISM Manufacturing PMI
    'PMI_EU':           ('CSCICP03EZM665S',  'monthly'),   # EU Manufacturing PMI
    'PMI_CN':           ('CSCICP03CNM665S',  'monthly'),   # China Manufacturing PMI
    'MONETARY_BASE':    ('BOGMBASE',         'monthly'),   # Monetary base (narrower than M2)
}

FRED_URL = 'https://api.stlouisfed.org/fred/series/observations'

# Trailing lookback window (days) for the daily incremental fetch, keyed by a
# series' declared frequency. 'daily' series publish same/next-day so 30 days
# is generous overlap. Everything else defaults to a much wider window because
# a series' PUBLICATION cadence can be far coarser than its own observation
# cadence — e.g. WM2NS ('M2') is dated weekly but is only actually published
# via FRED's H.6 release, which fires roughly once a month and bundles 4-5
# weeks of catch-up observations at once, with the oldest observation in a
# batch sometimes 40-50+ days old by the time it is first published. A 30-day
# window can never see those on the day the release lands (they are already
# older than the window), which permanently drops them — no exception is
# raised, the collector just reports fewer rows than actually exist.
# Example: quant.macro silently missed several WM2NS weeks (H.6 publish lag
# reached 43-50 days while the window only looked back 30).
DAILY_WINDOW_DAYS = {'daily': 30}
DEFAULT_DAILY_WINDOW_DAYS = 90


class FredMacroCollector(BaseCollector):
    name = 'fred_macro'
    expected_items = list(FRED_SERIES.keys())
    expected_items_key = 'metric'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.api_key = os.environ.get('FRED_API_KEY', '')
        if not self.api_key:
            raise ValueError("FRED_API_KEY not set in environment")

    def collect(self):
        end = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        if self.backfill:
            start = self.backfill_start or '2000-01-01'
            logger.info(f"[fred_macro] Backfilling from {start}")
        else:
            logger.info(
                "[fred_macro] Daily update (per-series window: daily=%dd, "
                "weekly/monthly=%dd)",
                DAILY_WINDOW_DAYS['daily'], DEFAULT_DAILY_WINDOW_DAYS,
            )

        for metric_key, (series_id, freq) in FRED_SERIES.items():
            try:
                if self.backfill:
                    series_start = start
                else:
                    window_days = DAILY_WINDOW_DAYS.get(freq, DEFAULT_DAILY_WINDOW_DAYS)
                    series_start = (
                        datetime.now(timezone.utc) - timedelta(days=window_days)
                    ).strftime('%Y-%m-%d')
                self._fetch_and_store(metric_key, series_id, series_start, end)
                time.sleep(0.5)  # ~2 req/sec (limit is 120/min)
            except Exception as e:
                logger.warning(f"  {metric_key} ({series_id}): FAILED - {e}")

        # Backfill: attempt to fill actuals for today's events now that FRED
        # series are freshly updated.  Non-fatal — a failure here never blocks
        # the macro collector completing its run.
        try:
            from quant.collectors.event_calendar import fill_event_actuals
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            n = fill_event_actuals(today)
            if n:
                logger.info("[fred_macro] fill_event_actuals(%s): %d rows updated", today, n)
        except Exception as e:
            logger.debug("[fred_macro] fill_event_actuals skipped (non-fatal): %s", e)

        # Keep the forward macro calendar populated with real FRED release dates
        # (PCE/PPI/GDP) so the report never has to invent events. Non-fatal.
        try:
            from quant.collectors.event_calendar import fetch_fred_release_events
            added = fetch_fred_release_events(window_days=45)
            if added:
                logger.info("[fred_macro] fetch_fred_release_events: %d new events", added)
        except Exception as e:
            logger.debug("[fred_macro] fetch_fred_release_events skipped (non-fatal): %s", e)

    def _fetch_and_store(self, metric_key: str, series_id: str,
                         start: str, end: str):
        """Fetch one FRED series and upsert into quant.macro."""
        try:
            resp = requests.get(FRED_URL, params={
                'series_id': series_id,
                'api_key': self.api_key,
                'file_type': 'json',
                'observation_start': start,
                'observation_end': end,
                'sort_order': 'asc',
            }, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            safe_msg = str(e).replace(self.api_key, '***') if self.api_key else str(e)
            raise RuntimeError(f"FRED API error for {series_id}: {safe_msg}") from None

        observations = resp.json().get('observations', [])

        # Look-ahead guard: capture each observation's actual first-publish
        # date (FRED initial-release vintage) so analysis can align on the
        # knowledge date instead of the observation date. Values themselves stay
        # latest-revised (current behaviour). Best-effort — a failed lookup
        # leaves released_at NULL, which COALESCE(released_at, ts) falls back on.
        time.sleep(0.5)  # politeness between the value + release-date calls
        release_dates = self._fetch_release_dates(series_id, start, end)

        rows = []
        for obs in observations:
            val = obs.get('value')
            if val in (None, '.', ''):
                continue
            try:
                rows.append({
                    'ts': obs['date'],
                    'metric': metric_key,
                    'value': float(val),
                    'released_at': release_dates.get(obs['date']),
                    'revision': 0,
                })
            except (ValueError, TypeError):
                continue

        if rows:
            count = self.upsert_rows(
                'quant.macro', rows,
                conflict_cols=['ts', 'metric'],
                update_cols=['value', 'released_at', 'revision'],
            )
            logger.info(f"  {metric_key}: {len(rows)} obs, {count} upserted")
        else:
            logger.info(f"  {metric_key}: no data")

    def _fetch_release_dates(self, series_id: str, start: str, end: str) -> dict:
        """Map observation date -> first-publish date (ISO 'YYYY-MM-DD').

        Uses FRED's initial-release vintage (output_type=4) across all realtime
        periods, where each row's `realtime_start` is the date that value was
        first published. Best-effort: returns {} on any error so the main value
        fetch still succeeds (released_at just stays NULL for that series).
        """
        try:
            resp = requests.get(FRED_URL, params={
                'series_id': series_id,
                'api_key': self.api_key,
                'file_type': 'json',
                'observation_start': start,
                'observation_end': end,
                'sort_order': 'asc',
                'output_type': 4,            # 4 = initial release only
                'realtime_start': '1776-07-04',
                'realtime_end': '9999-12-31',
            }, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            safe_msg = str(e).replace(self.api_key, '***') if self.api_key else str(e)
            logger.warning(f"  {series_id}: release-date fetch failed ({safe_msg}); released_at NULL")
            return {}

        dates = {}
        for obs in resp.json().get('observations', []):
            rt = obs.get('realtime_start')
            # FRED's sentinel minimum date means "first release unknown" — skip.
            if rt and rt != '1776-07-04':
                dates[obs['date']] = rt
        return dates


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('FRED macro data collector')
    collector = FredMacroCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
    )
    collector.run()
