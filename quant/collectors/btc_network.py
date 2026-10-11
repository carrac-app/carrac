"""
Bitcoin network fundamentals collector for Quant DB.

Sources: mempool.space public API (no auth, no API key required).

LICENSING POSTURE (grey zone):
  mempool.space ToS has no explicit commercial grant and no explicit commercial
  ban. The server software is AGPL-3.0 but that licence does NOT bind an
  arm's-length API consumer — we are not distributing or modifying the server
  software; we call its public HTTP API. Acceptable-use posture:
    • ONE call per endpoint per daily cron run (never polling/streaming).
    • Descriptive User-Agent identifying us and stating low volume.
    • Respect 429 responses (BaseCollector._request handles backoff).
  Do NOT increase call frequency without re-reading the ToS at
  https://mempool.space/terms-of-service.

ENDPOINTS used (HTTP 200, no auth headers):
  GET /api/v1/mining/reward-stats/144  → totalReward, totalFee (strings, satoshis)
  GET /api/v1/mining/hashrate/1y       → currentHashrate (H/s), difficulty array
  GET /api/mempool                     → vsize (virtual bytes in mempool)
  GET /api/blocks/tip/height           → bare integer (current block height)

METRICS written to quant.onchain (asset='BTC'):
  BTC_MINER_REVENUE_USD  int(totalReward)/1e8 × BTC_close_USD  [USD/day]  source: mempool+local
  BTC_TX_FEES_USD        int(totalFee)/1e8 × BTC_close_USD     [USD/day]  source: mempool+local
  BTC_HASHRATE           currentHashrate / 1e18                 [EH/s]     source: mempool.space
  BTC_DIFFICULTY_ADJ     (latest_epoch_adj − 1.0) × 100        [percent]  source: mempool.space
  BTC_MEMPOOL_VBYTES     mempool.vsize                          [vbytes]   source: mempool.space
  BTC_BLOCK_HEIGHT       current tip height (display-only)      [blocks]   source: mempool.space

USD-denominated metrics require a BTC close price from quant.price_daily.
If no close row exists, those two metrics are SKIPPED entirely (not written as 0).
Each endpoint is wrapped in its own try/except so a single failure never aborts the run.

Usage:
    python -m quant.collectors.btc_network [--dry-run]
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from carrac.version import APP_VERSION
from quant.collectors.base import BaseCollector
from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger('quant.collectors.btc_network')

USER_AGENT = f'Carrac/{APP_VERSION} (low-volume daily)'

REWARD_STATS_URL = 'https://mempool.space/api/v1/mining/reward-stats/144'
HASHRATE_URL     = 'https://mempool.space/api/v1/mining/hashrate/1y'
MEMPOOL_URL      = 'https://mempool.space/api/mempool'
HEIGHT_URL       = 'https://mempool.space/api/blocks/tip/height'

_HEADERS = {'User-Agent': USER_AGENT}


class BTCNetworkCollector(BaseCollector):
    """Collect BTC network fundamentals from mempool.space (daily, low-volume)."""

    name = 'btc_network'
    expected_items = ['BTC']
    expected_items_key = 'asset'

    def collect(self):
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        logger.info(f"[btc_network] Collecting BTC network fundamentals for {today}")

        rows = []

        # BTC close price — needed for USD-denominated metrics.
        # Queried ONCE up front; if unavailable, USD metrics are skipped.
        btc_close = self._fetch_btc_close()
        if btc_close is None:
            logger.warning(
                "[btc_network] No BTC close price in quant.price_daily "
                "— BTC_MINER_REVENUE_USD and BTC_TX_FEES_USD will be skipped"
            )

        # ── Endpoint 1: reward-stats/144 (miner revenue + fees, in satoshis) ──
        try:
            resp = requests.get(REWARD_STATS_URL, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            # totalReward and totalFee are JSON strings representing satoshis
            reward_btc = int(data['totalReward']) / 1e8
            fee_btc    = int(data['totalFee'])    / 1e8

            if btc_close is not None:
                rows.append({
                    'ts': today, 'asset': 'BTC',
                    'metric': 'BTC_MINER_REVENUE_USD',
                    'value': round(reward_btc * btc_close, 2),
                    'source': 'mempool+local',
                })
                rows.append({
                    'ts': today, 'asset': 'BTC',
                    'metric': 'BTC_TX_FEES_USD',
                    'value': round(fee_btc * btc_close, 2),
                    'source': 'mempool+local',
                })
            else:
                logger.info(
                    "[btc_network] Skipping BTC_MINER_REVENUE_USD / BTC_TX_FEES_USD (no BTC close)"
                )
        except Exception as e:
            logger.warning(f"[btc_network] reward-stats endpoint failed: {e}")

        # ── Endpoint 2: hashrate/1y (network hashrate + difficulty adjustment) ──
        try:
            resp = requests.get(HASHRATE_URL, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            # currentHashrate is in raw H/s; /1e18 converts to EH/s
            hashrate_ehs = data['currentHashrate'] / 1e18
            rows.append({
                'ts': today, 'asset': 'BTC',
                'metric': 'BTC_HASHRATE',
                'value': round(hashrate_ehs, 4),
                'source': 'mempool.space',
            })

            # Use the LAST (most recent) epoch's adjustment multiplier
            adj_entries = data.get('difficulty', [])
            if adj_entries:
                latest_adj = adj_entries[-1]['adjustment']
                rows.append({
                    'ts': today, 'asset': 'BTC',
                    'metric': 'BTC_DIFFICULTY_ADJ',
                    'value': round((latest_adj - 1.0) * 100, 4),
                    'source': 'mempool.space',
                })
        except Exception as e:
            logger.warning(f"[btc_network] hashrate endpoint failed: {e}")

        # ── Endpoint 3: mempool (vbytes backlog) ──────────────────────────────
        try:
            resp = requests.get(MEMPOOL_URL, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            rows.append({
                'ts': today, 'asset': 'BTC',
                'metric': 'BTC_MEMPOOL_VBYTES',
                'value': int(data['vsize']),
                'source': 'mempool.space',
            })
        except Exception as e:
            logger.warning(f"[btc_network] mempool endpoint failed: {e}")

        # ── Endpoint 4: tip height (bare integer, display-only) ───────────────
        try:
            resp = requests.get(HEIGHT_URL, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            height = int(resp.text.strip())
            rows.append({
                'ts': today, 'asset': 'BTC',
                'metric': 'BTC_BLOCK_HEIGHT',
                'value': height,
                'source': 'mempool.space',
            })
        except Exception as e:
            logger.warning(f"[btc_network] tip height endpoint failed: {e}")

        # ── Upsert all collected metrics ──────────────────────────────────────
        if rows:
            count = self.upsert_rows(
                'quant.onchain', rows,
                conflict_cols=['ts', 'asset', 'metric'],
                update_cols=['value', 'source'],
            )
            logger.info(
                f"[btc_network] {len(rows)} metrics collected for {today}, {count} rows upserted"
            )
        else:
            logger.warning("[btc_network] No metrics collected — all endpoints failed")

    def _fetch_btc_close(self) -> Optional[float]:
        """Return the most recent BTC close from quant.price_daily (Binance source).

        Returns None if the table has no qualifying row or the DB is unreachable.
        Callers must skip USD-denominated metrics when None is returned.
        """
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT close FROM quant.price_daily
                    WHERE asset = 'BTC' AND source = 'binance' AND close > 0
                    ORDER BY ts DESC LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    return float(row['close'])
                return None
        except Exception as e:
            logger.warning(f"[btc_network] Failed to fetch BTC close from price_daily: {e}")
            return None


if __name__ == '__main__':
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('BTC network fundamentals collector (mempool.space)')
    collector = BTCNetworkCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
        max_retries=args.max_retries,
    )
    collector.run()
