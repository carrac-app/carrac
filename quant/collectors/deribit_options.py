"""
Deribit options & futures data collector for Quant DB.

Fetches BTC and ETH options/futures data from Deribit public API (free, no auth).
Computes: put/call ratio, max pain, ATM IV, total options OI, DVOL,
          futures basis/APR, 25-delta skew, IV term structure, GEX, IV-RV premium.

Schedule: Every 6 hours (options change slowly).
Backfill: DVOL from 2021. Other metrics accumulate from day 1.

Usage:
    python -m quant.collectors.deribit_options
    python -m quant.collectors.deribit_options --backfill
    python -m quant.collectors.deribit_options --dry-run
"""

import logging
import math
import time
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from scipy.stats import norm

from carrac.db_pool import get_quant_conn as get_conn
from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.deribit_options')

DERIBIT_BASE = 'https://www.deribit.com/api/v2/public'
CURRENCIES = ['BTC', 'ETH']

# Risk-free rate approximation (used in Black-Scholes).
# Updated from Fed Funds in DB when available, falls back to this.
DEFAULT_RISK_FREE_RATE = 0.0425


class DeribitOptionsCollector(BaseCollector):
    name = 'deribit_options'
    expected_items = list(CURRENCIES)

    def collect(self):
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        for currency in CURRENCIES:
            try:
                raw_options, underlying = self._collect_options_snapshot(currency, ts)
                time.sleep(1)
            except Exception as e:
                logger.warning(f"  {currency} options snapshot: {e}")
                raw_options, underlying = None, None

            # Derived metrics from the options chain (no extra API calls)
            if raw_options and underlying:
                try:
                    self._compute_and_store_skew(raw_options, underlying, currency, ts)
                except Exception as e:
                    logger.warning(f"  {currency} skew: {e}")

                try:
                    self._compute_and_store_term_structure(raw_options, underlying, currency, ts)
                except Exception as e:
                    logger.warning(f"  {currency} term structure: {e}")

                try:
                    self._compute_and_store_gex(raw_options, underlying, currency, ts)
                except Exception as e:
                    logger.warning(f"  {currency} GEX: {e}")

            # Futures basis (2 new API calls per currency)
            try:
                self._collect_futures_basis(currency, ts)
                time.sleep(1)
            except Exception as e:
                logger.warning(f"  {currency} futures basis: {e}")

            # DVOL
            try:
                self._collect_dvol(currency)
                time.sleep(1)
            except Exception as e:
                logger.warning(f"  {currency} DVOL: {e}")

            # IV-RV premium (cross-table: dvol from derivatives, rvol from sentiment)
            try:
                self._compute_iv_rv_premium(currency, ts)
            except Exception as e:
                logger.warning(f"  {currency} IV-RV premium: {e}")

    def _collect_options_snapshot(self, currency: str, ts: str):
        """Fetch all active options and compute aggregate metrics.

        Returns (raw_data, underlying_price) for reuse by derived computations.
        """
        resp = requests.get(f'{DERIBIT_BASE}/get_book_summary_by_currency', params={
            'currency': currency,
            'kind': 'option',
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json().get('result', [])

        if not data:
            logger.warning(f"  {currency}: no options data")
            return None, None

        # Get underlying price from any option's underlying_price
        underlying = None
        for opt in data:
            up = opt.get('underlying_price')
            if up and up > 0:
                underlying = up
                break

        if not underlying:
            logger.warning(f"  {currency}: no underlying price found")
            return None, None

        # Separate calls and puts, aggregate OI
        call_oi = 0.0
        put_oi = 0.0
        total_oi_usd = 0.0

        # For max pain: group by expiry -> {strike: {call_oi, put_oi}}
        expiry_data = defaultdict(lambda: defaultdict(lambda: {'call_oi': 0.0, 'put_oi': 0.0}))

        # For ATM IV: find options closest to current price
        atm_candidates = []

        for opt in data:
            name = opt.get('instrument_name', '')
            oi = float(opt.get('open_interest', 0))
            mark_iv = opt.get('mark_iv', 0)

            if oi <= 0:
                continue

            # Parse instrument: BTC-28MAR26-85000-C
            parts = name.split('-')
            if len(parts) < 4:
                continue

            expiry_str = parts[1]
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue
            opt_type = parts[3]  # C or P

            oi_usd = oi * underlying

            if opt_type == 'C':
                call_oi += oi_usd
                expiry_data[expiry_str][strike]['call_oi'] += oi
            else:
                put_oi += oi_usd
                expiry_data[expiry_str][strike]['put_oi'] += oi

            total_oi_usd += oi_usd

            # Track ATM candidates (within 5% of spot)
            if mark_iv and mark_iv > 0 and abs(strike - underlying) / underlying < 0.05:
                atm_candidates.append((oi, mark_iv))

        # Put/Call ratio
        pc_ratio = put_oi / call_oi if call_oi > 0 else None

        # ATM IV (OI-weighted average of near-the-money options)
        iv_atm = None
        if atm_candidates:
            total_weight = sum(oi for oi, _ in atm_candidates)
            if total_weight > 0:
                iv_atm = sum(oi * iv for oi, iv in atm_candidates) / total_weight

        # Max pain for the nearest major expiry
        max_pain = self._compute_max_pain(expiry_data, underlying)

        # Upsert into derivatives
        self._upsert_options(ts, currency, {
            'options_put_call_ratio': round(pc_ratio, 4) if pc_ratio else None,
            'options_oi_usd': round(total_oi_usd, 2),
            'options_max_pain': round(max_pain, 2) if max_pain else None,
            'options_iv_atm': round(iv_atm, 2) if iv_atm else None,
        })

        logger.info(
            f"  {currency} options: P/C={pc_ratio:.3f}, "
            f"OI=${total_oi_usd:,.0f}, "
            f"MaxPain={max_pain:,.0f}, "
            f"ATM_IV={iv_atm:.1f}%"
            if all(v is not None for v in [pc_ratio, max_pain, iv_atm]) else
            f"  {currency} options: partial data stored"
        )

        return data, underlying

    def _compute_max_pain(self, expiry_data, underlying):
        """Compute max pain for the nearest expiry with significant OI."""
        if not expiry_data:
            return None

        best_expiry = None
        best_oi = 0
        for expiry, strikes in expiry_data.items():
            total = sum(
                s['call_oi'] + s['put_oi'] for s in strikes.values()
            )
            if total > best_oi:
                best_oi = total
                best_expiry = expiry

        if not best_expiry:
            return None

        strikes_data = expiry_data[best_expiry]
        all_strikes = sorted(strikes_data.keys())

        if not all_strikes:
            return None

        min_pain = float('inf')
        max_pain_strike = None

        for settlement in all_strikes:
            total_value = 0.0
            for strike, oi in strikes_data.items():
                call_itm = max(0, settlement - strike)
                total_value += call_itm * oi['call_oi']
                put_itm = max(0, strike - settlement)
                total_value += put_itm * oi['put_oi']

            if total_value < min_pain:
                min_pain = total_value
                max_pain_strike = settlement

        return max_pain_strike

    # ------------------------------------------------------------------
    # Futures basis / APR
    # ------------------------------------------------------------------

    def _collect_futures_basis(self, currency: str, ts: str):
        """Fetch futures and compute annualized basis (APR) vs spot."""
        # Get spot index price
        resp_idx = requests.get(f'{DERIBIT_BASE}/get_index_price', params={
            'index_name': f'{currency.lower()}_usd',
        }, timeout=15)
        resp_idx.raise_for_status()
        spot = resp_idx.json().get('result', {}).get('index_price')
        if not spot or spot <= 0:
            logger.warning(f"  {currency}: no index price for basis")
            return

        # Get futures book summary
        resp = requests.get(f'{DERIBIT_BASE}/get_book_summary_by_currency', params={
            'currency': currency,
            'kind': 'future',
        }, timeout=15)
        resp.raise_for_status()
        futures = resp.json().get('result', [])

        if not futures:
            return

        # Find the best quarterly future (highest OI, non-perpetual, > 30 days out)
        best_future = None
        best_oi = 0

        for f in futures:
            name = f.get('instrument_name', '')
            if 'PERPETUAL' in name:
                continue
            mark = f.get('mark_price')
            oi = f.get('open_interest', 0)
            if not mark or mark <= 0:
                continue

            # Parse expiry
            parts = name.split('-')
            if len(parts) < 2:
                continue
            try:
                exp_date = datetime.strptime(parts[1], '%d%b%y')
                days = (exp_date - datetime.now()).days
            except (ValueError, IndexError):
                continue

            if days < 30:
                continue

            if oi > best_oi:
                best_oi = oi
                best_future = {
                    'name': name,
                    'mark': mark,
                    'days': days,
                    'oi': oi,
                }

        if not best_future:
            # Fall back to any future > 7 days
            for f in futures:
                name = f.get('instrument_name', '')
                if 'PERPETUAL' in name:
                    continue
                mark = f.get('mark_price')
                if not mark or mark <= 0:
                    continue
                parts = name.split('-')
                if len(parts) < 2:
                    continue
                try:
                    exp_date = datetime.strptime(parts[1], '%d%b%y')
                    days = (exp_date - datetime.now()).days
                except (ValueError, IndexError):
                    continue
                if days > 7:
                    best_future = {'name': name, 'mark': mark, 'days': days}
                    break

        if not best_future:
            return

        basis_pct = ((best_future['mark'] - spot) / spot) * 100
        apr = basis_pct * (365.0 / best_future['days'])

        self._upsert_options(ts, currency, {
            'basis_pct': round(apr, 4),  # Store annualized APR
        })

        logger.info(
            f"  {currency} futures basis: {best_future['name']} "
            f"mark={best_future['mark']:,.0f}, spot={spot:,.0f}, "
            f"basis={basis_pct:+.3f}%, APR={apr:+.2f}% ({best_future['days']}d)"
        )

    # ------------------------------------------------------------------
    # 25-delta skew
    # ------------------------------------------------------------------

    def _compute_and_store_skew(self, options_data, underlying, currency, ts):
        """Compute 25-delta skew from the options chain.

        25-delta skew = IV of 25-delta puts - IV of 25-delta calls.
        We approximate 25-delta by selecting OTM options at 15-20% from spot.
        Positive skew = puts more expensive = bearish hedging demand.
        """
        # Group options with IV by expiry
        by_expiry = defaultdict(list)
        for opt in options_data:
            name = opt.get('instrument_name', '')
            mark_iv = opt.get('mark_iv', 0)
            oi = opt.get('open_interest', 0)
            if not mark_iv or mark_iv <= 0 or oi <= 0:
                continue
            parts = name.split('-')
            if len(parts) < 4:
                continue
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue
            by_expiry[parts[1]].append({
                'strike': strike,
                'iv': mark_iv,
                'oi': oi,
                'type': parts[3],  # C or P
            })

        # Find the nearest monthly expiry with enough options (> 14 days out)
        best_expiry = None
        best_days = float('inf')
        now = datetime.now()

        for exp_str, opts in by_expiry.items():
            try:
                exp_date = datetime.strptime(exp_str, '%d%b%y')
                days = (exp_date - now).days
            except ValueError:
                continue
            if 14 < days < best_days and len(opts) > 10:
                best_days = days
                best_expiry = exp_str

        if not best_expiry:
            return

        opts = by_expiry[best_expiry]

        # 25-delta approximation: OTM puts at 10-25% below spot, calls at 10-25% above
        otm_puts = [o for o in opts if o['type'] == 'P'
                    and 0.10 < (underlying - o['strike']) / underlying < 0.25]
        otm_calls = [o for o in opts if o['type'] == 'C'
                     and 0.10 < (o['strike'] - underlying) / underlying < 0.25]

        if not otm_puts or not otm_calls:
            return

        # OI-weighted average IV
        put_total_oi = sum(o['oi'] for o in otm_puts)
        call_total_oi = sum(o['oi'] for o in otm_calls)

        if put_total_oi <= 0 or call_total_oi <= 0:
            return

        put_iv = sum(o['iv'] * o['oi'] for o in otm_puts) / put_total_oi
        call_iv = sum(o['iv'] * o['oi'] for o in otm_calls) / call_total_oi
        skew = put_iv - call_iv

        self._upsert_options(ts, currency, {
            'skew_25d': round(skew, 2),
        })

        logger.info(f"  {currency} skew: put_iv={put_iv:.1f}%, call_iv={call_iv:.1f}%, skew={skew:+.1f}% ({best_expiry})")

    # ------------------------------------------------------------------
    # IV term structure
    # ------------------------------------------------------------------

    def _compute_and_store_term_structure(self, options_data, underlying, currency, ts):
        """Compute IV term structure buckets (7d, 30d, 90d).

        Positive slope = contango (normal), negative = backwardation (fear).
        """
        # Collect ATM options with IV, grouped by expiry
        by_expiry = defaultdict(list)
        for opt in options_data:
            name = opt.get('instrument_name', '')
            mark_iv = opt.get('mark_iv', 0)
            oi = opt.get('open_interest', 0)
            if not mark_iv or mark_iv <= 0 or oi <= 0:
                continue
            parts = name.split('-')
            if len(parts) < 4:
                continue
            try:
                strike = float(parts[2])
            except (ValueError, IndexError):
                continue
            # ATM: within 5% of spot
            if abs(strike - underlying) / underlying > 0.05:
                continue
            by_expiry[parts[1]].append({
                'iv': mark_iv,
                'oi': oi,
            })

        now = datetime.now()
        # Bucket targets: 7d, 30d, 90d
        buckets = {'7d': (3, 14), '30d': (14, 45), '90d': (45, 180)}
        iv_by_bucket = {}

        for exp_str, opts in by_expiry.items():
            try:
                exp_date = datetime.strptime(exp_str, '%d%b%y')
                days = (exp_date - now).days
            except ValueError:
                continue
            if days <= 0:
                continue

            for bucket, (lo, hi) in buckets.items():
                if lo <= days < hi:
                    total_oi = sum(o['oi'] for o in opts)
                    if total_oi > 0:
                        weighted_iv = sum(o['iv'] * o['oi'] for o in opts) / total_oi
                        # Keep the closest to bucket center
                        center = (lo + hi) / 2
                        existing = iv_by_bucket.get(bucket)
                        if existing is None or abs(days - center) < existing[1]:
                            iv_by_bucket[bucket] = (weighted_iv, abs(days - center))

        iv_7d = iv_by_bucket.get('7d', (None,))[0]
        iv_30d = iv_by_bucket.get('30d', (None,))[0]
        iv_90d = iv_by_bucket.get('90d', (None,))[0]

        # Term structure slope
        slope = None
        if iv_7d and iv_90d and iv_7d > 0:
            slope = (iv_90d - iv_7d) / iv_7d

        cols = {}
        if iv_7d is not None:
            cols['iv_7d'] = round(iv_7d, 2)
        if iv_30d is not None:
            cols['iv_30d'] = round(iv_30d, 2)
        if iv_90d is not None:
            cols['iv_90d'] = round(iv_90d, 2)
        if slope is not None:
            cols['term_structure_slope'] = round(slope, 4)

        if cols:
            self._upsert_options(ts, currency, cols)
            parts = [f"{k}={v}" for k, v in cols.items()]
            logger.info(f"  {currency} term structure: {', '.join(parts)}")

    # ------------------------------------------------------------------
    # Gamma Exposure (GEX)
    # ------------------------------------------------------------------

    def _compute_and_store_gex(self, options_data, underlying, currency, ts):
        """Compute net Gamma Exposure (GEX) and flip point.

        GEX = sum(gamma * OI * spot^2 / 100) across all options.
        Calls: dealers are typically long gamma (positive GEX).
        Puts: dealers are typically short gamma (negative GEX contribution).
        Flip point: strike where cumulative net GEX crosses zero.
        """
        now = datetime.now()
        r = DEFAULT_RISK_FREE_RATE

        # Try to get Fed Funds rate from DB
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT value FROM quant.macro
                    WHERE metric = 'FED_FUNDS'
                    ORDER BY ts DESC LIMIT 1
                """)
                row = cur.fetchone()
                if row and row.get('value'):
                    r = float(row['value']) / 100.0  # Convert from percentage
        except Exception:
            pass

        # Compute per-strike GEX
        strike_gex = defaultdict(float)  # strike -> net GEX in USD

        for opt in options_data:
            name = opt.get('instrument_name', '')
            mark_iv = opt.get('mark_iv', 0)
            oi = opt.get('open_interest', 0)

            if not mark_iv or mark_iv <= 0 or oi <= 0:
                continue

            parts = name.split('-')
            if len(parts) < 4:
                continue

            try:
                strike = float(parts[2])
                exp_date = datetime.strptime(parts[1], '%d%b%y')
            except (ValueError, IndexError):
                continue

            opt_type = parts[3]  # C or P
            T = max((exp_date - now).days / 365.0, 1 / 365.0)  # Time to expiry in years
            sigma = mark_iv / 100.0  # Convert from percentage

            if sigma <= 0 or strike <= 0:
                continue

            # Black-Scholes gamma
            try:
                d1 = (math.log(underlying / strike) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
                gamma = norm.pdf(d1) / (underlying * sigma * math.sqrt(T))
            except (ValueError, ZeroDivisionError):
                continue

            # GEX contribution in USD
            # Contract size = 1 BTC/ETH on Deribit
            # Dealers are typically: long gamma on calls, short gamma on puts
            # GEX = gamma * OI * spot^2 / 100
            gex_usd = gamma * oi * underlying ** 2 / 100.0

            if opt_type == 'C':
                strike_gex[strike] += gex_usd   # Positive: dealers long gamma
            else:
                strike_gex[strike] -= gex_usd   # Negative: dealers short gamma

        if not strike_gex:
            return

        # Net GEX
        gex_net = sum(strike_gex.values())

        # Find flip point (where cumulative GEX crosses zero)
        sorted_strikes = sorted(strike_gex.keys())
        cumulative = 0.0
        flip_price = None
        prev_strike = None
        prev_cum = 0.0

        for strike in sorted_strikes:
            cumulative += strike_gex[strike]
            if prev_strike is not None and prev_cum * cumulative < 0:
                # Sign change — interpolate
                if cumulative != prev_cum:
                    flip_price = prev_strike + (strike - prev_strike) * abs(prev_cum) / (abs(prev_cum) + abs(cumulative))
                else:
                    flip_price = (prev_strike + strike) / 2
                break
            prev_cum = cumulative
            prev_strike = strike

        self._upsert_options(ts, currency, {
            'gex_net': round(float(gex_net), 2),
            'gex_flip_price': round(float(flip_price), 2) if flip_price else None,
        })

        flip_str = f"flip={flip_price:,.0f}" if flip_price else "no flip"
        logger.info(f"  {currency} GEX: net={gex_net:,.0f} USD, {flip_str}, spot={underlying:,.0f}")

    # ------------------------------------------------------------------
    # IV-RV premium
    # ------------------------------------------------------------------

    def _compute_iv_rv_premium(self, currency: str, ts: str):
        """Compute IV minus RV (variance risk premium).

        Positive = options overpricing realized moves (sellers' market).
        Negative = options underpricing (dangerous, buyers' market).
        """
        with get_conn() as conn:
            cur = conn.cursor()

            # Get latest DVOL
            cur.execute("""
                SELECT dvol FROM quant.derivatives
                WHERE asset = %s AND dvol IS NOT NULL
                ORDER BY ts DESC LIMIT 1
            """, (currency,))
            row = cur.fetchone()
            if not row or not row.get('dvol'):
                return
            dvol = float(row['dvol'])

            # Get latest realized vol
            cur.execute("""
                SELECT value FROM quant.sentiment
                WHERE metric = %s AND asset = %s
                ORDER BY ts DESC LIMIT 1
            """, (f'REALIZED_VOL_{currency}', currency))
            row = cur.fetchone()
            if not row or not row.get('value'):
                return
            rvol = float(row['value'])

        premium = dvol - rvol

        self._upsert_options(ts, currency, {
            'iv_rv_premium': round(premium, 2),
        })

        logger.info(f"  {currency} IV-RV: DVOL={dvol:.1f}, RVOL={rvol:.1f}, premium={premium:+.1f}")

    # ------------------------------------------------------------------
    # DVOL
    # ------------------------------------------------------------------

    def _collect_dvol(self, currency: str):
        """Fetch DVOL (Deribit Volatility Index) history."""
        if self.backfill:
            start_str = self.backfill_start or '2021-01-01'
            start_ms = int(datetime.strptime(start_str, '%Y-%m-%d').timestamp() * 1000)
        else:
            start_ms = int((datetime.now() - timedelta(days=7)).timestamp() * 1000)

        end_ms = int(datetime.now().timestamp() * 1000)

        resp = requests.get(f'{DERIBIT_BASE}/get_volatility_index_data', params={
            'currency': currency,
            'resolution': 86400,  # daily
            'start_timestamp': start_ms,
            'end_timestamp': end_ms,
        }, timeout=15)
        resp.raise_for_status()

        result = resp.json().get('result', {})
        candles = result.get('data', [])
        if not candles:
            candles = result if isinstance(result, list) else []

        rows = []
        for candle in candles:
            if not isinstance(candle, list) or len(candle) < 5:
                continue
            ts = datetime.fromtimestamp(candle[0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
            dvol_close = candle[4]  # close
            if dvol_close and dvol_close > 0:
                rows.append({'ts': ts, 'asset': currency, 'dvol': dvol_close})

        if rows:
            self._upsert_options_batch(rows, ['dvol'])
            logger.info(f"  {currency} DVOL: {len(rows)} days")

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _upsert_options(self, ts, asset, cols_dict):
        """Upsert options columns into quant.derivatives."""
        if self.dry_run:
            logger.info(f"[{self.name}] DRY RUN: {asset} {cols_dict} for {ts}")
            return

        update_cols = [k for k, v in cols_dict.items() if v is not None]
        if not update_cols:
            return

        with get_conn() as conn:
            cur = conn.cursor()
            cols = ['ts', 'asset'] + update_cols
            vals = [ts, asset] + [cols_dict[c] for c in update_cols]
            placeholders = ', '.join(['%s'] * len(cols))
            col_str = ', '.join(cols)
            set_str = ', '.join(f'{c} = EXCLUDED.{c}' for c in update_cols)

            cur.execute(f"""
                INSERT INTO quant.derivatives ({col_str})
                VALUES ({placeholders})
                ON CONFLICT (ts, asset) DO UPDATE SET {set_str}
            """, vals)
            self._rows_upserted += cur.rowcount

    def _upsert_options_batch(self, rows, update_cols):
        """Batch upsert for DVOL and similar time-series data."""
        if not rows or self.dry_run:
            if self.dry_run and rows:
                logger.info(f"[{self.name}] DRY RUN: {len(rows)} rows")
            return

        with get_conn() as conn:
            cur = conn.cursor()
            for row in rows:
                cols = ['ts', 'asset'] + update_cols
                vals = [row['ts'], row['asset']] + [row.get(c) for c in update_cols]
                placeholders = ', '.join(['%s'] * len(cols))
                col_str = ', '.join(cols)
                set_str = ', '.join(f'{c} = EXCLUDED.{c}' for c in update_cols)

                cur.execute(f"""
                    INSERT INTO quant.derivatives ({col_str})
                    VALUES ({placeholders})
                    ON CONFLICT (ts, asset) DO UPDATE SET {set_str}
                """, vals)
                self._rows_upserted += cur.rowcount


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('Deribit options data collector')
    collector = DeribitOptionsCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
    )
    collector.run()
