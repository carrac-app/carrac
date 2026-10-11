"""
Pattern matching — find historical periods similar to the current chart.

Algorithm:
1. For each timeframe (1D, 3D, 1W), take that TF's last N bars as reference
2. Slide window across that TF's own history (apples-to-apples)
3. Pearson correlation on normalized price SHAPE (not returns)
4. Pool candidates from all TFs, filter r >= 0.85, remove overlapping
5. Return top 5 + what happened next (20 bars)
"""

import logging
import time
from typing import List, Dict, Optional

import numpy as np

logger = logging.getLogger('carrac.ai.pattern')

_SEARCH_TIMEFRAMES = ['1D', '3D', '1W']


def find_similar_patterns(
    asset: str,
    timeframe: str = '1D',
    window_size: int = 20,
    lookahead: int = 20,
    min_correlation: float = 0.85,
    max_results: int = 5,
) -> Dict:
    """Find historical periods with similar price action across all timeframes.

    Each TF is searched independently using its own current window,
    so comparisons are always same-scale (20 1D bars vs 20 1D bars, etc).
    """
    start = time.time()

    all_candidates = []
    global_max_r = -1.0
    total_windows_searched = 0
    current_prices_by_tf = {}
    current_ohlc_by_tf = {}

    for search_tf in _SEARCH_TIMEFRAMES:
        bars = _get_historical_bars(asset, search_tf)
        if bars is None or len(bars) < window_size + lookahead + 50:
            logger.info(f"Pattern match: skipping {search_tf} — {len(bars) if bars else 0} bars")
            continue

        closes = np.array([b['close'] for b in bars], dtype=np.float64)
        timestamps = [b.get('timestamp') or b.get('date', '') for b in bars]

        # Current window for THIS timeframe
        current = closes[-window_size:]
        current_returns = _normalize_shape(current)
        if current_returns is None:
            logger.info(f"Pattern match: cannot normalize current window for {search_tf}")
            continue

        # Full close prices (1:1 with bars) for sparkline + overlay line
        current_prices_by_tf[search_tf] = [round(float(p), 2) for p in current]
        # Full OHLC for ghost candle overlay
        current_ohlc_by_tf[search_tf] = [
            {'o': round(b['open'], 2), 'h': round(b['high'], 2),
             'l': round(b['low'], 2), 'c': round(b['close'], 2)}
            for b in bars[-window_size:]
        ]

        # Slide over history (exclude tail to avoid self-match)
        search_end = len(closes) - window_size - lookahead
        if search_end < window_size:
            continue

        logger.info(f"Pattern match: searching {asset}/{search_tf} — {len(bars)} bars, {search_end} windows")

        tf_candidates = []
        tf_max_r = -1.0

        for i in range(0, search_end):
            window = closes[i:i + window_size]
            window_returns = _normalize_shape(window)
            if window_returns is None:
                continue

            r = _pearson(current_returns, window_returns)
            if r > tf_max_r:
                tf_max_r = r
            if r >= min_correlation:
                future_start = i + window_size
                future_end = min(future_start + lookahead, len(closes))
                future = closes[future_start:future_end]
                outcome_pct = ((future[-1] / window[-1]) - 1) * 100 if len(future) > 0 else 0

                tf_candidates.append({
                    'index': i,
                    'correlation': round(float(r), 4),
                    'date': str(timestamps[i]),
                    'end_date': str(timestamps[i + window_size - 1]),
                    'outcome_pct': round(float(outcome_pct), 2),
                    'forward_return': round(float(outcome_pct), 2),
                    'outcome_bars': len(future),
                    'entry_price': round(float(window[-1]), 2),
                    'prices': [round(float(p), 2) for p in window],
                    'future_prices': [round(float(p), 2) for p in future],
                    'source_tf': search_tf,
                })

        tf_candidates.sort(key=lambda x: x['correlation'], reverse=True)
        tf_filtered = _remove_overlapping(tf_candidates, window_size)
        all_candidates.extend(tf_filtered)
        total_windows_searched += search_end

        if tf_max_r > global_max_r:
            global_max_r = tf_max_r

        logger.info(f"Pattern match: {search_tf} — {len(tf_candidates)} raw, {len(tf_filtered)} after dedup, max_r={tf_max_r:.4f}")

    all_candidates.sort(key=lambda x: x['correlation'], reverse=True)
    matches = all_candidates[:max_results]

    elapsed = int((time.time() - start) * 1000)
    avg_fwd = sum(m['outcome_pct'] for m in matches) / len(matches) if matches else 0

    # Use the requested TF's current prices for the sparkline header;
    # fall back to first available TF
    primary_prices = current_prices_by_tf.get(timeframe) or next(iter(current_prices_by_tf.values()), [])

    logger.info(f"Pattern match: TOTAL {total_windows_searched} windows across {len(_SEARCH_TIMEFRAMES)} TFs, "
                f"{len(all_candidates)} candidates, {len(matches)} returned, max_r={global_max_r:.4f}, elapsed={elapsed}ms")

    return {
        'matches': matches,
        'current_prices': primary_prices,
        'current_prices_by_tf': current_prices_by_tf,
        'current_ohlc_by_tf': current_ohlc_by_tf,
        'avg_forward_return': round(avg_fwd, 2),
        'max_correlation': round(global_max_r, 4) if global_max_r > -1 else 0,
        'asset': asset,
        'timeframe': timeframe,
        'window_size': window_size,
        'lookahead': lookahead,
        'bars_searched': total_windows_searched,
        'elapsed_ms': elapsed,
    }


def _get_historical_bars(asset: str, timeframe: str) -> Optional[List[Dict]]:
    """Get OHLCV bars from the database."""
    try:
        from carrac.services import get_services
        db = get_services().db

        if timeframe in ('1h', '2h', '4h'):
            tf_data = db.get_ltf_multi_timeframe_data(asset)
            tf_key = timeframe.lower()
            df = tf_data.get(tf_key)
        else:
            tf_data = db.get_multi_timeframe_data(asset)
            tf_map = {'1D': '1D', '3D': '3D', '1W': '1W'}
            tf_key = tf_map.get(timeframe, '1D')
            df = tf_data.get(tf_key)

        if df is None or df.empty:
            return None

        bars = []
        for idx, row in df.iterrows():
            bars.append({
                'timestamp': str(idx),
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': float(row.get('volume', 0)),
            })
        return bars
    except Exception as e:
        logger.error(f"Pattern match data error: {e}")
        return None


def _normalize_shape(prices: np.ndarray) -> Optional[np.ndarray]:
    """Normalize price series to z-scored levels for shape comparison.

    Maps prices to 0-1 range then z-scores, so correlation measures
    visual shape similarity (trajectory), not just return patterns.
    """
    if len(prices) < 2:
        return None
    pmin, pmax = prices.min(), prices.max()
    if pmax == pmin:
        return None
    normalized = (prices - pmin) / (pmax - pmin)
    std = np.std(normalized)
    if std == 0:
        return None
    return (normalized - np.mean(normalized)) / std


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    """Fast Pearson correlation between two normalized arrays."""
    n = len(a)
    if n != len(b) or n == 0:
        return 0.0
    return float(np.dot(a, b) / n)


def _remove_overlapping(candidates: List[Dict], window_size: int) -> List[Dict]:
    """Remove overlapping matches, keeping highest correlation."""
    if not candidates:
        return []
    used = set()
    result = []
    for c in candidates:
        idx = c['index']
        # Check if any bar in this window is already claimed
        overlap = False
        for j in range(idx, idx + window_size):
            if j in used:
                overlap = True
                break
        if not overlap:
            result.append(c)
            for j in range(idx, idx + window_size):
                used.add(j)
    return result
