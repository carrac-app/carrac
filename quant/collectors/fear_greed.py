"""
Crypto Fear & Greed Index collector for Quant DB.

Source: Alternative.me API (free, no API key needed).
Stores in quant.sentiment with metric='FEAR_GREED'.
Backfill: ~2 years available (pass limit=0 for all).

Usage:
    python -m quant.collectors.fear_greed
    python -m quant.collectors.fear_greed --backfill
"""

import logging
import requests
from datetime import datetime, timezone

from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.fear_greed')

API_URL = 'https://api.alternative.me/fng/'


class FearGreedCollector(BaseCollector):
    name = 'fear_greed'
    expected_items = ['FEAR_GREED']
    expected_items_key = 'metric'

    def collect(self):
        if self.backfill:
            limit = 0  # 0 = all available history
            logger.info("[fear_greed] Backfilling all available history")
        else:
            limit = 7
            logger.info("[fear_greed] Daily update (last 7 days)")

        resp = requests.get(API_URL, params={
            'limit': limit,
            'format': 'json',
        }, timeout=15)
        resp.raise_for_status()

        entries = resp.json().get('data', [])
        rows = []
        for entry in entries:
            ts = datetime.fromtimestamp(int(entry['timestamp']), tz=timezone.utc).strftime('%Y-%m-%d')
            rows.append({
                'ts': ts,
                'metric': 'FEAR_GREED',
                'asset': '_GLOBAL_',
                'value': int(entry['value']),
                'source': 'alternative_me',
            })

        if rows:
            count = self.upsert_rows(
                'quant.sentiment', rows,
                conflict_cols=['ts', 'metric', 'asset'],
                update_cols=['value', 'source'],
            )
            logger.info(f"  Fear & Greed: {len(rows)} days, {count} upserted")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('Fear & Greed Index collector')
    collector = FearGreedCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
    )
    collector.run()
