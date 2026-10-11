"""Market-analysis helpers with no Flask or blueprint dependencies."""

import logging

import requests

logger = logging.getLogger('carrac.analysis')


def fetch_fear_greed_index():
    """
    Fetch Fear & Greed Index from alternative.me API.

    Returns:
        dict with value (0-100), classification, and timestamp
    """
    try:
        response = requests.get('https://api.alternative.me/fng/', timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get('data'):
                fng = data['data'][0]
                if fng.get('value') is not None:
                    return {
                        'value': int(fng['value']),
                        'classification': fng.get('value_classification') or None,
                        'timestamp': fng.get('timestamp')
                    }
        reason = f'alternative.me returned {response.status_code} with no usable reading'
    except Exception as e:
        reason = f'alternative.me unreachable: {e}'

    # Return None rather than a fabricated 50/'Neutral': every
    # downstream consumer (precompute's hourly snapshot, morning_engine's
    # afternoon delta, report_blocks) already tests for None, and a fake
    # value would defeat those guards.
    logger.warning("Fear & Greed unavailable: %s", reason)
    return {'value': None, 'classification': None, 'timestamp': None, 'error': reason}


def calculate_mansfield_rs(asset_prices, benchmark_prices, period=52):
    """
    Calculate Mansfield Relative Strength vs benchmark.

    RS = (Asset / Benchmark) normalized over period

    Returns:
        dict with rs_value, rs_ma, interpretation
    """
    if len(asset_prices) < period or len(benchmark_prices) < period:
        return None

    # Calculate ratio
    ratio = asset_prices / benchmark_prices
    recent_ratio = ratio.iloc[-period:]

    # Current RS
    current_rs = ratio.iloc[-1]

    # RS Moving Average (for trend)
    rs_ma = recent_ratio.mean()

    # Normalize to 0-100 scale based on period range
    period_min = recent_ratio.min()
    period_max = recent_ratio.max()

    if period_max > period_min:
        normalized_rs = ((current_rs - period_min) / (period_max - period_min)) * 100
    else:
        normalized_rs = 50

    # Interpretation
    if normalized_rs > 70 and current_rs > rs_ma:
        interpretation = 'strong_outperform'
    elif normalized_rs > 50:
        interpretation = 'outperform'
    elif normalized_rs > 30:
        interpretation = 'neutral'
    else:
        interpretation = 'underperform'

    # Calculate % change vs benchmark over period
    pct_vs_benchmark = ((ratio.iloc[-1] / ratio.iloc[-period]) - 1) * 100

    return {
        'rs_normalized': round(normalized_rs, 1),
        'rs_raw': round(current_rs, 6),
        'rs_ma': round(rs_ma, 6),
        'above_ma': current_rs > rs_ma,
        'interpretation': interpretation,
        'pct_vs_benchmark': round(pct_vs_benchmark, 2)
    }
