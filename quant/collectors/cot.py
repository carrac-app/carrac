"""
CFTC Commitment of Traders (COT) collector for Quant DB.

Fetches weekly positioning data from the CFTC SODA API (free, no auth).
Tracks speculative and commercial positioning for BTC, ETH, S&P 500,
gold, crude oil, and USD index.

Schedule: Weekly (Saturday 06:00 UTC — data published Friday 3:30 PM ET).
Backfill: BTC from Dec 2017, macro contracts from 2000.

Usage:
    python -m quant.collectors.cot
    python -m quant.collectors.cot --backfill
    python -m quant.collectors.cot --dry-run
"""

import logging
import time
import requests
from datetime import datetime, timedelta

from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.cot')


def _published_date(report_date: str) -> str | None:
    """Look-ahead guard: COT positioning is reported as-of Tuesday but not
    published until the following Friday (3:30 PM ET). Approximate the publish
    date (released_at) as report_date + 3 days so analysis aligns on when the
    data was actually knowable, not the as-of date. Returns None on a malformed
    date (COALESCE then falls back to ts)."""
    try:
        d = datetime.strptime(report_date, '%Y-%m-%d').date()
        return (d + timedelta(days=3)).isoformat()
    except ValueError:
        return None

CFTC_BASE = 'https://publicreporting.cftc.gov/resource/jun7-fc8e.json'

# Contracts to track: (display_name, cftc_code, backfill_start)
CONTRACTS = {
    'BTC':   ('133741', '2017-12-01'),
    'ETH':   ('146021', '2021-01-01'),
    'SP500': ('13874A', '2000-01-01'),
    'GOLD':  ('088691', '2000-01-01'),
    'OIL':   ('067651', '2000-01-01'),
    'DXY':   ('098662', '2000-01-01'),
}

# Fields to fetch from CFTC API
SELECT_FIELDS = ','.join([
    'report_date_as_yyyy_mm_dd',
    'open_interest_all',
    'noncomm_positions_long_all',
    'noncomm_positions_short_all',
    'comm_positions_long_all',
    'comm_positions_short_all',
    'change_in_noncomm_long_all',
    'change_in_noncomm_short_all',
])


class CotCollector(BaseCollector):
    name = 'cot'
    expected_items = list(CONTRACTS.keys())

    def collect(self):
        for asset_name, (code, default_start) in CONTRACTS.items():
            try:
                self._fetch_contract(asset_name, code, default_start)
                time.sleep(1)
            except Exception as e:
                logger.warning(f"  COT {asset_name}: {e}")

    def _fetch_contract(self, asset_name: str, code: str, default_start: str):
        """Fetch COT data for one contract and store derived metrics."""
        if self.backfill:
            start = self.backfill_start or default_start
            limit = 50000  # Get everything
        else:
            start = None
            limit = 8  # Last ~2 months of weekly data

        params = {
            '$where': f"cftc_contract_market_code='{code}'",
            '$select': SELECT_FIELDS,
            '$order': 'report_date_as_yyyy_mm_dd DESC',
            '$limit': limit,
        }
        if start:
            params['$where'] += f" AND report_date_as_yyyy_mm_dd >= '{start}'"

        resp = requests.get(CFTC_BASE, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            logger.info(f"  COT {asset_name}: no data")
            return

        rows = []
        for entry in data:
            ts = entry.get('report_date_as_yyyy_mm_dd', '')
            if not ts:
                continue
            released = _published_date(ts)

            try:
                oi = float(entry.get('open_interest_all', 0))
                spec_long = float(entry.get('noncomm_positions_long_all', 0))
                spec_short = float(entry.get('noncomm_positions_short_all', 0))
                comm_long = float(entry.get('comm_positions_long_all', 0))
                comm_short = float(entry.get('comm_positions_short_all', 0))
            except (ValueError, TypeError):
                continue

            spec_net = spec_long - spec_short
            comm_net = comm_long - comm_short
            spec_long_pct = (spec_long / oi * 100) if oi > 0 else 0

            # Store 3 metrics per contract per week
            rows.extend([
                {
                    'ts': ts,
                    'metric': f'COT_{asset_name}_SPEC_NET',
                    'asset': '_GLOBAL_',
                    'value': spec_net,
                    'source': 'cftc',
                    'released_at': released,
                },
                {
                    'ts': ts,
                    'metric': f'COT_{asset_name}_COMM_NET',
                    'asset': '_GLOBAL_',
                    'value': comm_net,
                    'source': 'cftc',
                    'released_at': released,
                },
                {
                    'ts': ts,
                    'metric': f'COT_{asset_name}_SPEC_LONG_PCT',
                    'asset': '_GLOBAL_',
                    'value': round(spec_long_pct, 2),
                    'source': 'cftc',
                    'released_at': released,
                },
            ])

        if rows:
            count = self.upsert_rows(
                'quant.sentiment', rows,
                conflict_cols=['ts', 'metric', 'asset'],
                update_cols=['value', 'source', 'released_at'],
            )
            n_weeks = len(rows) // 3
            logger.info(f"  COT {asset_name}: {n_weeks} weeks, {count} upserted")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('CFTC Commitment of Traders collector')
    collector = CotCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
    )
    collector.run()
