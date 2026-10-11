"""On-chain stablecoin supply collector. Replaces quant.collectors.defillama:
reads USDT/USDC totalSupply() off Ethereum + Tron public JSON-RPC (no commercial-
licensed feed). SOURCE SWAP — same metrics, same quant.sentiment table, same wiring.
No --backfill (on-chain has no cheap history); existing defillama rows ARE the
z-score warmup — they remain in quant.sentiment untouched.

Ethereum RPC: 5-way no-key fallback (publicnode → drpc → mevblocker → blastapi
→ llamarpc), browser User-Agent. Tron: TronGrid.
Coverage: ETH+Tron ≈ 86% USDT; ETH-only ≈ 69% USDC (Solana USDC is not counted).
Partial-failure guard: each metric is isolated in its own try/except; only
successfully-fetched metrics are upserted — no zero rows are ever written.

Usage:
    python -m quant.collectors.stablecoin_onchain
    python -m quant.collectors.stablecoin_onchain --dry-run
"""

import logging
import os
import requests
from datetime import datetime, timezone

from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.stablecoin_onchain')

# ── ETH JSON-RPC endpoints (free, no-key) ──────────
# Order = reliability seen in probing. publicnode/drpc/mevblocker/blastapi all
# returned eth_call results from both browser and python User-Agents; llamarpc
# kept as a final fallback (reachable from some networks where the others are blocked). rpc.ankr.com dropped —
# it now requires an API key. A browser-ish UA (RPC_HEADERS) is sent because
# some public gateways 403 the default python-requests/urllib UA.
ETH_RPCS = [
    'https://ethereum-rpc.publicnode.com',
    'https://eth.drpc.org',
    'https://rpc.mevblocker.io',
    'https://eth-mainnet.public.blastapi.io',
    'https://eth.llamarpc.com',
]

RPC_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
}

# ── Contract addresses ────────────────────────────────────────────────────────
USDT_ETH  = '0xdAC17F958D2ee523a2206206994597C13D831ec7'
USDC_ETH  = '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48'  # EIP-1967 proxy; totalSupply() resolves
USDT_TRON = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'

# ── ABI selectors ─────────────────────────────────────────────────────────────
SEL_TOTAL_SUPPLY = '0x18160ddd'   # totalSupply()
SEL_BALANCE_OF   = '0x70a08231'   # balanceOf(address)

# ── Tether ETH treasury (circulating = totalSupply − balanceOf(treasury)) ────
# Tron treasury subtraction (small float; avoids base58→hex trap)
ETH_TREASURY = '0x5754284f345afc66a98fbb0a0afe71e0f007b949'
USDT_ETH_TREASURY_DATA = SEL_BALANCE_OF + '0' * 24 + ETH_TREASURY[2:].lower()

# ── Tron API ─────────────────────────────────────────────────────────────────
TRON_API     = 'https://api.trongrid.io/wallet/triggerconstantcontract'
TRON_DUMMY   = 'T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb'  # dummy owner (read-only call)

# ── Precision ─────────────────────────────────────────────────────────────────
DEC = 1_000_000  # Both USDT-ETH and USDC-ETH use 6 decimals

# ── Dominance denominator ─────────────────────────────────────────────────────
GLOBAL_MCAP_URL = 'https://api.coinpaprika.com/v1/global'


class StablecoinOnchainCollector(BaseCollector):
    name = 'stablecoin_onchain'
    expected_items = ['STABLECOIN_USDT_SUPPLY', 'STABLECOIN_USDC_SUPPLY']
    expected_items_key = 'metric'  # default is 'asset'; stablecoins are keyed by metric

    # ── ETH JSON-RPC call ─────────────────────────────────────────────────────

    def _eth_call(self, to: str, data: str) -> int:
        """Call eth_call on all ETH RPCs (3-way fallback). Returns raw integer.

        Args:
            to:   Contract address (checksummed or lowercase)
            data: Hex-encoded ABI call data (function selector + params)

        Returns:
            Integer result decoded from the hex response.

        Raises:
            RuntimeError: If all RPCs fail or return empty.
        """
        body = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'eth_call',
            'params': [{'to': to, 'data': data}, 'latest'],
        }
        last_error = None
        for url in ETH_RPCS:
            try:
                r = requests.post(url, json=body, headers=RPC_HEADERS, timeout=20)
                r.raise_for_status()
                res = r.json().get('result')
                if res and res != '0x':
                    return int(res, 16)
                last_error = f'empty result from {url}'
            except Exception as e:
                last_error = f'{url}: {e}'
        raise RuntimeError(f'all ETH RPCs failed ({last_error})')

    # ── Tron totalSupply() ────────────────────────────────────────────────────

    def _tron_total_supply(self, contract: str) -> int:
        """Call totalSupply() on a TRC-20 contract via TronGrid.

        Returns raw integer (6 decimals for USDT-Tron).
        Reads TRON_PRO_API_KEY from env (optional; raises 403 without it on
        heavy usage but works unauthenticated at daily rates).
        """
        headers = dict(RPC_HEADERS)
        key = os.environ.get('TRON_PRO_API_KEY', '')
        if key:
            headers['TRON-PRO-API-KEY'] = key
        body = {
            'owner_address': TRON_DUMMY,
            'contract_address': contract,
            'function_selector': 'totalSupply()',
            'parameter': '',
            'visible': True,
        }
        r = requests.post(TRON_API, json=body, headers=headers, timeout=20)
        r.raise_for_status()
        return int(r.json()['constant_result'][0], 16)

    # ── Main collect ──────────────────────────────────────────────────────────

    def collect(self):
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        rows = []

        # USDT: ETH circulating (totalSupply − treasury) + Tron total
        try:
            eth_total   = self._eth_call(USDT_ETH, SEL_TOTAL_SUPPLY)
            eth_treasury = self._eth_call(USDT_ETH, USDT_ETH_TREASURY_DATA)
            eth_circ    = (eth_total - eth_treasury) / DEC
            tron_circ   = self._tron_total_supply(USDT_TRON) / DEC
            rows.append(self._row(today, 'STABLECOIN_USDT_SUPPLY', eth_circ + tron_circ))
        except Exception as e:
            logger.warning(f'[stablecoin_onchain] USDT: {e}')

        # USDC: ETH totalSupply only (Solana USDC is not counted)
        try:
            usdc_raw = self._eth_call(USDC_ETH, SEL_TOTAL_SUPPLY)
            rows.append(self._row(today, 'STABLECOIN_USDC_SUPPLY', usdc_raw / DEC))
        except Exception as e:
            logger.warning(f'[stablecoin_onchain] USDC: {e}')

        # Partial-failure guard: only upsert if at least one metric succeeded
        if rows:
            self.upsert_rows(
                'quant.sentiment', rows,
                conflict_cols=['ts', 'metric', 'asset'],
                update_cols=['value', 'source'],
            )

        # Dominance always runs after supply (reads freshest supply from DB)
        try:
            self._compute_stablecoin_dominance(today)
        except Exception as e:
            logger.warning(f'[stablecoin_onchain] dominance: {e}')

    # ── Row builder ───────────────────────────────────────────────────────────

    @staticmethod
    def _row(ts: str, metric: str, value: float) -> dict:
        """Build a quant.sentiment row dict. asset='_GLOBAL_' matches defillama.py."""
        return {
            'ts':     ts,
            'metric': metric,
            'asset':  '_GLOBAL_',
            'value':  round(value, 2),
            'source': 'onchain',
        }

    # ── Stablecoin dominance ──────────────────────────────────────────────────

    def _compute_stablecoin_dominance(self, today: str):
        """STABLECOIN_DOMINANCE = (USDT + USDC supply) / total_crypto_mcap * 100.

        Lifted from defillama.py._compute_stablecoin_dominance (daily branch)
        with source changed to 'onchain+coinpaprika'.  Reads the freshest supply
        rows already in quant.sentiment (written by collect() above or by previous
        successful runs), so it remains correct even on partial USDT/USDC failures.
        """
        from carrac.db_pool import get_quant_conn as get_conn

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT metric, value FROM quant.sentiment
                WHERE metric IN ('STABLECOIN_USDT_SUPPLY', 'STABLECOIN_USDC_SUPPLY')
                  AND ts = (SELECT MAX(ts) FROM quant.sentiment
                            WHERE metric = 'STABLECOIN_USDT_SUPPLY')
            """)
            supply = {r['metric']: float(r['value']) for r in cur.fetchall()}

        total_stable = (
            supply.get('STABLECOIN_USDT_SUPPLY', 0)
            + supply.get('STABLECOIN_USDC_SUPPLY', 0)
        )
        if total_stable == 0:
            logger.warning('[stablecoin_onchain] No stablecoin supply data — skipping dominance')
            return

        resp = requests.get(
            GLOBAL_MCAP_URL, timeout=15,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; QuaintDB/1.0)'},
        )
        resp.raise_for_status()
        total_mcap = resp.json().get('market_cap_usd', 0)
        if total_mcap == 0:
            logger.warning('[stablecoin_onchain] CoinPaprika returned 0 market cap — skipping dominance')
            return

        dominance = round(total_stable / total_mcap * 100, 4)
        self.upsert_rows(
            'quant.sentiment',
            [{'ts': today, 'metric': 'STABLECOIN_DOMINANCE', 'asset': '_GLOBAL_',
              'value': dominance, 'source': 'onchain+coinpaprika'}],
            conflict_cols=['ts', 'metric', 'asset'],
            update_cols=['value', 'source'],
        )
        logger.info(
            f'[stablecoin_onchain] Dominance {dominance:.4f}% '
            f'(stable={total_stable / 1e9:.1f}B / mcap={total_mcap / 1e12:.2f}T)'
        )


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('On-chain stablecoin supply collector')
    StablecoinOnchainCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
        max_retries=args.max_retries,
    ).run()
