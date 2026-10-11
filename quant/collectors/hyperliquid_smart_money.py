"""
Hyperliquid smart-money positioning collector.

Seeds a top-N wallet set from the Hyperliquid leaderboard, fetches open
positions per wallet via the clearinghouseState API, and computes a per-asset
SMART_MONEY_BIAS — a 0-100 oscillator:

  0   = all smart-money wallets are short
  50  = balanced (equal longs and shorts by dollar value)
  100 = all smart-money wallets are long

Bias methodology (degeneracy-resistant):
  For each asset across all top-N wallets:
    1. Sum absolute positionValue per side (longs, shorts) — uncapped.
    2. Per-wallet cap: limit each wallet's |positionValue| contribution to
       25% of the asset's uncapped gross, attenuating (not equalizing) a
       single whale's dominance — with extreme size gaps bias can still skew.
    3. gross = capped_longs + capped_shorts
    4. net   = capped_longs − capped_shorts
    5. Guards: skip the asset if fewer than 3 distinct wallets hold it,
       OR gross < $1,000,000.
    6. bias_raw = net / gross  ∈ [−1, 1]
    7. bias = round((bias_raw + 1) / 2 * 100, 1)

Stores one `quant.onchain` row per qualifying asset:
  {ts, asset: coin, metric: 'SMART_MONEY_BIAS', value: bias, source: 'hyperliquid'}

License: Hyperliquid ToS (June 2026) scopes only the web Interface;
no data-license regime; raw market data = non-copyrightable fact.
Risk is operational (leaderboard endpoint undocumented), not legal.

Usage:
    python -m quant.collectors.hyperliquid_smart_money
    python -m quant.collectors.hyperliquid_smart_money --dry-run
"""

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import requests

from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.hyperliquid_smart_money')

# ── Endpoints ────────────────────────────────────────────────────────────────

LEADERBOARD_URL = 'https://stats-data.hyperliquid.xyz/Mainnet/leaderboard'
INFO_URL = 'https://api.hyperliquid.xyz/info'

# ── Constants ────────────────────────────────────────────────────────────────

# Number of wallets to seed from the leaderboard
TOP_N = 50

# Wallets to use when the undocumented leaderboard endpoint is unavailable,
# from HL_SMART_MONEY_WALLETS (comma-separated addresses). Empty by default:
# with no leaderboard and no configured wallets the run is skipped.
FALLBACK_WALLETS_ENV = 'HL_SMART_MONEY_WALLETS'


def configured_fallback_wallets() -> List[str]:
    """Addresses listed in HL_SMART_MONEY_WALLETS, or an empty list."""
    raw = os.getenv(FALLBACK_WALLETS_ENV, '')
    return [w.strip() for w in raw.split(',') if w.strip()]

# Bias methodology parameters
WHALE_CAP_PCT = 0.25      # per-wallet cap as fraction of uncapped gross
MIN_WALLETS = 3           # minimum distinct wallets to write a row
MIN_GROSS_USD = 1_000_000 # minimum capped gross (USD) to write a row

# Politeness: avoid silent 429 (Hyperliquid rate limit is 1200 weight/min;
# 50 calls is trivial, but a small sleep keeps us well clear).
POLITENESS_SLEEP = 0.1    # seconds between per-wallet info calls

UPSERT_TABLE = 'quant.onchain'


# ── Collector ────────────────────────────────────────────────────────────────

class HyperliquidSmartMoneyCollector(BaseCollector):
    """Daily collector for Hyperliquid smart-money positioning bias."""

    name = 'hyperliquid_smart_money'
    expected_items: List[str] = []   # dynamic — depends on what positions exist
    expected_items_key = 'asset'

    def collect(self) -> None:
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        wallets = self._fetch_wallets()
        if not wallets:
            logger.warning(
                f'[{self.name}] No wallets: leaderboard unavailable and '
                f'{FALLBACK_WALLETS_ENV} is empty — skipping this run'
            )
            return
        logger.info(f'[{self.name}] Using {len(wallets)} wallets')

        positions_by_asset = self._fetch_all_positions(wallets)

        rows = self._compute_bias_rows(today, positions_by_asset)

        if rows:
            count = self.upsert_rows(
                UPSERT_TABLE,
                rows,
                conflict_cols=['ts', 'asset', 'metric'],
                update_cols=['value', 'source'],
            )
            logger.info(f'[{self.name}] {len(rows)} qualifying assets, {count} upserted')
        else:
            logger.warning(f'[{self.name}] No qualifying assets computed')

    # ── Leaderboard seeding ───────────────────────────────────────────

    def _fetch_wallets(self) -> List[str]:
        """Fetch top-N wallets by allTime PnL from the leaderboard.

        Falls back to the wallets in HL_SMART_MONEY_WALLETS on any network or
        parse error, or if the leaderboard returns no rows; that list is empty
        unless configured.
        """
        try:
            resp = requests.get(
                LEADERBOARD_URL,
                timeout=30,
                headers={'User-Agent': 'Mozilla/5.0 (compatible; QuaintDB/1.0)'},
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data.get('leaderboardRows', [])

            if not rows:
                logger.warning(f'[{self.name}] Empty leaderboard — using fallback wallets')
                return configured_fallback_wallets()

            def _alltime_pnl(row: dict) -> float:
                for window, perf in row.get('windowPerformances', []):
                    if window == 'allTime':
                        try:
                            return float(perf.get('pnl', 0) or 0)
                        except (ValueError, TypeError):
                            return 0.0
                return 0.0

            rows_sorted = sorted(rows, key=_alltime_pnl, reverse=True)
            addresses = [
                r['ethAddress']
                for r in rows_sorted[:TOP_N]
                if r.get('ethAddress')
            ]
            if addresses:
                logger.info(f'[{self.name}] Leaderboard seeded {len(addresses)} wallets')
                return addresses

            logger.warning(f'[{self.name}] No valid addresses in leaderboard — using fallback')
        except Exception as e:
            logger.warning(
                f'[{self.name}] Leaderboard fetch failed: {e} — using fallback wallets'
            )

        return configured_fallback_wallets()

    # ── Per-wallet position fetching ──────────────────────────────────

    def _fetch_all_positions(
        self, wallets: List[str]
    ) -> Dict[str, List[Tuple[str, float, bool]]]:
        """Fetch positions for all wallets.

        Returns positions_by_asset: {coin → [(wallet_addr, pos_value_abs, is_long), …]}

        One bad wallet (any exception) never aborts the run — it is logged
        and skipped, other wallets still contribute.
        """
        positions_by_asset: Dict[str, List[Tuple[str, float, bool]]] = defaultdict(list)

        for wallet in wallets:
            try:
                pos_list = self._fetch_wallet_positions(wallet)
                for coin, pos_value_abs, is_long in pos_list:
                    positions_by_asset[coin].append((wallet, pos_value_abs, is_long))
            except Exception as e:
                logger.warning(f'[{self.name}] Wallet {wallet[:12]}…: {e}')
            finally:
                time.sleep(POLITENESS_SLEEP)

        return positions_by_asset

    def _fetch_wallet_positions(
        self, wallet_address: str
    ) -> List[Tuple[str, float, bool]]:
        """Fetch and parse open positions for a single wallet.

        Returns list of (coin, pos_value_abs, is_long).

        Notes:
        - positionValue is already an absolute USD string (not signed).
        - szi is the signed decimal size: positive = long, negative = short.
        - liquidationPx may be null/None — we do not use it (guard is implicit
          since we never read that field).
        - leverage is an OBJECT {type, value}, not a scalar — we ignore it.
        """
        resp = requests.post(
            INFO_URL,
            json={'type': 'clearinghouseState', 'user': wallet_address},
            timeout=15,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; QuaintDB/1.0)'},
        )
        resp.raise_for_status()
        data = resp.json()

        result: List[Tuple[str, float, bool]] = []
        for ap in data.get('assetPositions', []):
            try:
                pos = ap['position']
                coin: str = pos['coin']
                szi = float(pos['szi'])
                if szi == 0.0:
                    continue   # flat — no open position
                pos_value = float(pos['positionValue'])
                if pos_value <= 0.0:
                    continue   # safety: positionValue should be positive
                is_long = szi > 0
                result.append((coin, pos_value, is_long))
            except (KeyError, ValueError, TypeError) as e:
                logger.debug(f'[{self.name}] Skipping position entry: {e}')

        return result

    # ── Bias computation ──────────────────────────────────────────────

    def _compute_bias_rows(
        self,
        today: str,
        positions_by_asset: Dict[str, List[Tuple[str, float, bool]]],
    ) -> List[dict]:
        """Compute SMART_MONEY_BIAS for each qualifying asset.

        See module docstring for the full methodology.
        """
        rows: List[dict] = []

        for coin, wallet_positions in positions_by_asset.items():
            # Guard: require at least MIN_WALLETS distinct wallets
            distinct_wallets = {wp[0] for wp in wallet_positions}
            if len(distinct_wallets) < MIN_WALLETS:
                logger.debug(
                    f'[{self.name}] {coin}: {len(distinct_wallets)} wallet(s) '
                    f'< {MIN_WALLETS} — skipping'
                )
                continue

            # Step 1: uncapped gross (sum of all absolute positionValues)
            uncapped_gross = sum(pv for _, pv, _ in wallet_positions)

            # Step 2: per-wallet cap at 25% of uncapped gross
            cap_per_wallet = WHALE_CAP_PCT * uncapped_gross

            capped_longs = 0.0
            capped_shorts = 0.0
            for _wallet_addr, pos_value_abs, is_long in wallet_positions:
                capped = min(pos_value_abs, cap_per_wallet)
                if is_long:
                    capped_longs += capped
                else:
                    capped_shorts += capped

            gross = capped_longs + capped_shorts

            # Guard: minimum gross $1M (after capping)
            if gross < MIN_GROSS_USD:
                logger.debug(
                    f'[{self.name}] {coin}: capped gross ${gross:,.0f} '
                    f'< ${MIN_GROSS_USD:,} — skipping'
                )
                continue

            net = capped_longs - capped_shorts
            bias_raw = net / gross                        # ∈ [−1, 1]
            bias = round((bias_raw + 1) / 2 * 100, 1)   # → [0, 100]

            rows.append({
                'ts': today,
                'asset': coin,
                'metric': 'SMART_MONEY_BIAS',
                'value': bias,
                'source': 'hyperliquid',
            })

        return rows


# ── CLI entry-point ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('Hyperliquid smart-money positioning collector')
    HyperliquidSmartMoneyCollector(
        dry_run=args.dry_run,
    ).run()
