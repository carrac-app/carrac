#!/usr/bin/env python3
"""
scripts/validate_liq_heatmap.py — validation harness.

Replays persisted liquidation events (quant.liquidation_events) against the
modeled heatmap's own persisted snapshots (quant.liq_heatmap_snapshots) and
reports what fraction of ACTUAL liquidation events landed inside the
model's top-N-intensity buckets, vs a uniform-random baseline.

Usage:
    python -m scripts.validate_liq_heatmap --symbol BTC
    python -m scripts.validate_liq_heatmap --symbol BTC --top-n 20 --days 14
    python -m scripts.validate_liq_heatmap --synthetic     # self-test, no DB/network

Methodology
===========
For every persisted heatmap snapshot ("cycle"), the harness takes the top-N
intensity buckets from that cycle's OWN sparse bands (already
threshold-filtered server-side) and checks whether each subsequently
observed liquidation event -- one that occurred strictly after that cycle's
timestamp and before the next cycle's -- falls inside any of them. An event
whose price falls entirely outside that cycle's own +/-band is excluded from
both the numerator and denominator (the model literally had no coverage
there, so it can neither be credited nor blamed for that event).

    uniform baseline = (number of top-N buckets used) / (total buckets in
    that cycle's band), averaged across cycles -- the expected hit
    fraction if liquidations were spread uniformly across the band instead
    of clustering where the model predicts.

This replays against the SAME persisted snapshots the API/frontend already
serve, so the number reported is exactly "did our own painted top-intensity
levels predict where real forced liquidations subsequently happened,"
against real history -- no extra model computation, no re-fetching Binance.

Honesty gate: quant.liquidation_events and quant.liq_heatmap_snapshots are
BOTH new tables introduced with the liquidation heatmap. If there isn't enough persisted
history yet (the WS feed only just started buffering events; the worker
only just started computing cycles), this script prints 0/0 and says so
plainly -- it does NOT fabricate a validation number. --synthetic runs the
identical scoring function against a synthetic dataset with a known
embedded pattern, to demonstrate the harness's math actually distinguishes
signal from noise while real history accumulates. That synthetic run is
NOT a real validation result and is labeled as such in its own output.
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Pure scoring (no I/O) — testable in isolation
# ---------------------------------------------------------------------------

def _price_in_band(price: float, band: dict) -> bool:
    return band['price_low'] <= price < band['price_high']


def score_events_against_snapshots(events: List[dict], snapshots: List[dict],
                                    top_n: int = 20) -> Dict[str, Optional[float]]:
    """events: [{'ts': datetime, 'price': float}, ...] (any order).
    snapshots: [{'ts': datetime, 'band_low': float, 'band_high': float,
                 'bands': [{'price_low','price_high','intensity_long',
                 'intensity_short'}, ...], 'n_buckets': int}, ...] (any order).

    Returns {observed_hits, observed_total, observed_fraction,
    uniform_fraction, n_snapshots_used}. `observed_fraction` /
    `uniform_fraction` are None when there is nothing to score (empty
    inputs, or every event fell outside every cycle's band).
    """
    result = {
        'observed_hits': 0, 'observed_total': 0, 'observed_fraction': None,
        'uniform_fraction': None, 'n_snapshots_used': 0,
    }
    if not events or not snapshots:
        return result

    snaps = sorted(snapshots, key=lambda s: s['ts'])
    hits = 0
    total = 0
    uniform_fracs = []

    for i, snap in enumerate(snaps):
        window_start = snap['ts']
        window_end = snaps[i + 1]['ts'] if i + 1 < len(snaps) else None

        bands = sorted(
            snap.get('bands') or [],
            key=lambda b: (b.get('intensity_long', 0) + b.get('intensity_short', 0)),
            reverse=True,
        )[:top_n]
        if not bands:
            continue
        n_buckets = snap.get('n_buckets') or 150
        uniform_fracs.append(min(1.0, len(bands) / float(n_buckets)))

        band_low = snap.get('band_low')
        band_high = snap.get('band_high')

        for ev in events:
            if ev['ts'] <= window_start:
                continue
            if window_end is not None and ev['ts'] > window_end:
                continue
            if band_low is not None and band_high is not None:
                if ev['price'] < band_low or ev['price'] >= band_high:
                    continue  # outside this cycle's modeled band entirely
            total += 1
            if any(_price_in_band(ev['price'], b) for b in bands):
                hits += 1

    result['observed_hits'] = hits
    result['observed_total'] = total
    result['observed_fraction'] = (hits / total) if total else None
    result['uniform_fraction'] = (sum(uniform_fracs) / len(uniform_fracs)) if uniform_fracs else None
    result['n_snapshots_used'] = len(uniform_fracs)
    return result


# ---------------------------------------------------------------------------
# Live data (I/O)
# ---------------------------------------------------------------------------

def fetch_real_data(symbol: str, days: int = 14):
    from carrac.db_pool import get_quant_conn as get_conn
    from carrac.liq_heatmap import DEFAULT_N_BUCKETS

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT ts, price FROM quant.liquidation_events
            WHERE asset = %s AND ts >= NOW() - (%s || ' days')::interval
            ORDER BY ts ASC
        """, (symbol, days))
        events = [{'ts': row['ts'], 'price': float(row['price'])} for row in cur.fetchall()]

        cur.execute("""
            SELECT cycle_ts, price, band_low, band_high, bands
            FROM quant.liq_heatmap_snapshots
            WHERE symbol = %s AND cycle_ts >= NOW() - (%s || ' days')::interval
            ORDER BY cycle_ts ASC
        """, (symbol, days))
        snapshots = [
            {
                'ts': row['cycle_ts'],
                'band_low': float(row['band_low']) if row['band_low'] is not None else None,
                'band_high': float(row['band_high']) if row['band_high'] is not None else None,
                'bands': row['bands'] or [],
                'n_buckets': DEFAULT_N_BUCKETS,
            }
            for row in cur.fetchall()
        ]
    return events, snapshots


# ---------------------------------------------------------------------------
# Synthetic self-test (no I/O) — proves the harness's math works, NOT a
# real validation result
# ---------------------------------------------------------------------------

def build_synthetic_dataset(seed: int = 42):
    """Liquidation events cluster tightly around a handful of 'hot' price
    levels that the synthetic snapshots also paint as high-intensity, plus
    uniform noise elsewhere -- a known embedded pattern the scorer should
    detect as a clear lift over the uniform baseline."""
    rng = random.Random(seed)
    base_ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    hot_prices = [95.0, 105.0, 112.0]

    snapshots = []
    for h in range(0, 24 * 5, 4):  # every 4h for 5 days
        snap_ts = base_ts + timedelta(hours=h)
        bands = [
            {'price_low': hp - 0.5, 'price_high': hp + 0.5,
             'intensity_long': 0.9, 'intensity_short': 0.0}
            for hp in hot_prices
        ]
        snapshots.append({
            'ts': snap_ts, 'band_low': 75.0, 'band_high': 125.0,
            'bands': bands, 'n_buckets': 150,
        })

    events = []
    for _ in range(300):
        t = base_ts + timedelta(hours=rng.uniform(0, 24 * 5))
        if rng.random() < 0.7:
            price = rng.choice(hot_prices) + rng.uniform(-0.3, 0.3)  # signal
        else:
            price = rng.uniform(75.0, 125.0)  # noise
        events.append({'ts': t, 'price': price})
    events.sort(key=lambda e: e['ts'])
    return events, snapshots


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--symbol', default='BTC')
    parser.add_argument('--days', type=int, default=14)
    parser.add_argument('--top-n', type=int, default=20)
    parser.add_argument('--synthetic', action='store_true',
                         help='Run against a synthetic self-test dataset instead of the '
                              'live DB (no network/DB required; NOT a real validation number).')
    args = parser.parse_args()

    if args.synthetic:
        print("=== SYNTHETIC SELF-TEST (proves the scoring math works -- NOT a real validation number) ===")
        events, snapshots = build_synthetic_dataset()
    else:
        print(f"=== Live validation: {args.symbol}, last {args.days}d, top-{args.top_n} buckets ===")
        try:
            events, snapshots = fetch_real_data(args.symbol, days=args.days)
        except Exception as e:
            print(f"COULD NOT REACH THE QUANT DB -- cannot compute a validation number: {e}")
            print("Run this from an environment with quant DB connectivity "
                  "(e.g. inside the carrac/carrac-worker container), or use --synthetic "
                  "to sanity-check the scoring logic itself without any DB/network.")
            return

    result = score_events_against_snapshots(events, snapshots, top_n=args.top_n)

    print(f"Snapshots used:        {result['n_snapshots_used']}")
    print(f"Events considered:     {result['observed_total']} (in-band, post-first-cycle)")

    if result['observed_total'] == 0:
        print("INSUFFICIENT DATA -- cannot compute a validation number yet.")
        if not args.synthetic:
            print("quant.liquidation_events / quant.liq_heatmap_snapshots are both new "
                  "tables as of this release -- give the WS persistence + worker cycles "
                  "time to accumulate real history, then re-run this script.")
        return

    print(f"Observed hit fraction: {result['observed_fraction']:.3f}")
    print(f"Uniform baseline:      {result['uniform_fraction']:.3f}")
    if result['uniform_fraction']:
        lift = result['observed_fraction'] / result['uniform_fraction']
        print(f"Lift over uniform:     {lift:.2f}x")


if __name__ == '__main__':
    main()
