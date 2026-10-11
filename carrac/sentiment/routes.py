"""
Sentiment routes: market sentiment, sentiment history, fear & greed.
"""

import os
import logging
import time
import threading
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from carrac.auth.decorators import auth_or_key_required

logger = logging.getLogger('carrac.sentiment')

sentiment_bp = Blueprint('sentiment', __name__)

# Response-level cache for /api/market-sentiment
_sentiment_response_cache = {}
_sentiment_response_lock = threading.Lock()
_SENTIMENT_RESPONSE_TTL = 120  # 2 minutes

# Wall-clock ceilings for one _compute_sentiment() fan-out.
# The request budget sits under every sane client timeout; the background
# stale-while-revalidate thread gets longer because nobody is waiting on it.
_SENTIMENT_BUDGET_REQUEST_S = float(os.environ.get('SENTIMENT_COMPUTE_BUDGET_S', '12'))
_SENTIMENT_BUDGET_BACKGROUND_S = float(os.environ.get('SENTIMENT_BACKGROUND_BUDGET_S', '45'))

# 'fred' and 'regime' both bottleneck on the same FRED upstream and
# the same 2-slot `_FRED_REQUEST_SEMAPHORE` (data_sources/fred.py) —
# regime.py::gather_regime_inputs() makes 3 more sequential FRED calls on top
# of fred's own 5-way get_all() fan-out. Reordering the `tasks` dict below
# would NOT fix starvation between them: every future is already submitted
# concurrently before the result loop runs, so each task's real completion
# time is independent of which key is checked first —
# `future.result(timeout=remaining)` just waits for the SAME wall-clock
# deadline regardless of iteration order (proved by
# TestFredDependentTasksHaveASubBudget in test_sentiment_upstream_degradation.py).
#
# This constant is the mechanism for capping fred/regime's wait to a FRACTION
# of the request budget instead of the whole thing — but the default is 1.0
# (no cap; they get the full budget, same as every other task). Reasoning:
# the goal is fewer blank 'fred'/'regime'
# widgets, and lowering this below 1.0 makes that WORSE, not better —
# a FRED task that would complete at, say, 8s (well inside the 12s
# default budget) would succeed uncapped; capping it to a 6s sub-budget forces it
# into a degraded fallback it didn't need. What actually lowers the
# alarm count is bounding get_all() itself so a single slow series can't hold
# the whole key hostage (FredClient.get_all() timeout_s, data_sources/fred.py)
# — that turns full-key failures into partial degradations without spending
# any of the outer budget early. The fraction knob stays env-tunable
# (`SENTIMENT_FRED_SUBBUDGET_FRACTION`) for a future scenario where some OTHER
# task needs a guaranteed minimum slice of wall clock ahead of fred/regime's
# worst case — an operator could dial this down temporarily for that, but
# shipping it below 1.0 by default trades successful completions for faster
# failures, which is the opposite of the goal.
_SENTIMENT_FRED_SUBBUDGET_FRACTION = float(os.environ.get('SENTIMENT_FRED_SUBBUDGET_FRACTION', '1.0'))
_FRED_DEPENDENT_KEYS = frozenset({'fred', 'regime'})


# --- Color helpers ---

def _get_fear_greed_color(value) -> str:
    if value is None:
        return '#8b949e'   # unknown — same grey as an unavailable MVRV
    if value < 25:
        return '#f85149'
    elif value < 40:
        return '#f0883e'
    elif value < 60:
        return '#d29922'
    return '#3fb950'


def _format_ls_ratio(data: dict) -> dict:
    if not data:
        return {'ratio': None, 'color': '#8b949e'}
    ratio = data.get('longShortRatio')
    if ratio is None:
        return {'ratio': None, 'color': '#8b949e'}
    ratio = float(ratio)
    if ratio < 0.9:
        color = '#3fb950'
    elif ratio > 1.1:
        color = '#f85149'
    else:
        color = '#d29922'
    return {
        'ratio': round(ratio, 2),
        'longPct': round(float(data.get('longAccount', 0.5)) * 100, 1),
        'shortPct': round(float(data.get('shortAccount', 0.5)) * 100, 1),
        'color': color,
    }


def _get_alt_season_color(index) -> str:
    if index is None:
        return '#8b949e'   # unknown — same grey as an unavailable MVRV
    if index < 25:
        return '#f85149'
    elif index > 75:
        return '#3fb950'
    return '#d29922'


def _get_mvrv_color(zscore) -> str:
    if zscore is None:
        return '#8b949e'
    if zscore < 0:
        return '#3fb950'
    elif zscore < 3:
        return '#d29922'
    elif zscore < 7:
        return '#f0883e'
    return '#f85149'


def _get_funding_rate_color(rate) -> str:
    if rate is None:
        return '#8b949e'
    rate_pct = rate * 100
    if rate_pct > 0.05:
        return '#ff6b6b'
    elif rate_pct < -0.05:
        return '#51cf66'
    return '#adb5bd'


def _get_db():
    """Return the shared OHLCVDatabase instance."""
    from carrac.services import get_services
    return get_services().db


def _fetch_fear_greed():
    """Fetch Fear & Greed index from alternative.me. Late import to avoid circular deps."""
    # A fetch_fear_greed_index helper exists in carrac/analysis.py; this local copy avoids
    # an import-time circular dependency with database.py
    import requests
    try:
        resp = requests.get('https://api.alternative.me/fng/', timeout=10)
        if resp.status_code == 200:
            data = resp.json().get('data', [{}])[0]
            if data.get('value') is not None:
                return {
                    'value': int(data['value']),
                    'classification': data.get('value_classification') or None,
                }
        reason = f'alternative.me returned {resp.status_code} with no usable reading'
    except Exception as e:
        reason = f'alternative.me unreachable: {e}'
    # Never fall back to a fabricated {'value': 50, 'classification':
    # 'Neutral'} reading: every downstream `is not None` guard
    # (report_blocks, afternoon_delta) is written to catch None, and a
    # fabricated value would quietly defeat it.
    logger.warning("Fear & Greed unavailable: %s", reason)
    return {'value': None, 'classification': None, 'error': reason}


def _capture(message: str) -> None:
    """Send one message to the error tracker. Seam so the alarm is testable.

    Same shape as carrac/reports/prior_day.py::_capture — the established
    human-reaching alarm in this codebase.
    """
    import sentry_sdk
    sentry_sdk.capture_message(message, level="error")


def _alarm_budget_exhausted(key: str) -> None:
    """Running out of wall clock is abnormal — a human must see it.

    A logger.warning here would be a decision not to know: the 79.30s request
    that prompted this fix was fully logged and nobody read it for a day.
    The error tracker groups on the message, so a sustained outage is one issue with a
    rising count rather than a flood.
    """
    msg = (f"Sentiment compute budget exhausted before '{key}' returned — "
           "serving a degraded payload (upstream-degraded)")
    logger.error(msg)
    try:
        _capture(msg)
    except Exception as e:                       # never break the response path
        logger.debug("Budget-exhaustion beacon failed (swallowed): %s", e)


def _compute_sentiment(budget_s: float | None = None) -> dict:
    """Fetch all sentiment sources in parallel and return the assembled result dict.

    Called both on the request thread (cold first-hit) and from the background
    stale-while-revalidate refresh thread.  Raises on total failure so callers
    can distinguish a result from an error.

    `budget_s` is a WALL-CLOCK ceiling for the whole fan-out, not a per-task
    timeout — see the shutdown(wait=False) note below for why that distinction
    is the entire point.  Defaults to the request budget.
    """
    from concurrent.futures import ThreadPoolExecutor
    db = _get_db()
    total_budget = _SENTIMENT_BUDGET_REQUEST_S if budget_s is None else budget_s
    deadline = time.monotonic() + total_budget
    # See _SENTIMENT_FRED_SUBBUDGET_FRACTION above.
    fred_subbudget = total_budget * _SENTIMENT_FRED_SUBBUDGET_FRACTION

    def _fetch_fg():
        fg = _fetch_fear_greed()
        value = fg.get('value')
        out = {
            'value': value,
            'classification': fg.get('classification'),
            'color': _get_fear_greed_color(value),
        }
        if value is None:
            out['error'] = fg.get('error') or 'unavailable'
        return out

    def _fetch_ls():
        ls_ratios = db.fetch_long_short_ratio(['BTC', 'ETH', 'SOL'])
        return {
            sym: _format_ls_ratio(ls_ratios.get(sym, {}))
            for sym in ('BTC', 'ETH', 'SOL')
        }

    def _fetch_alt():
        alt_season = db.calculate_alt_season_index()
        # Pass the unavailable state through untouched. A `.get(…, 50)`
        # here is what turned "we have no idea" into a published "neutral".
        index = alt_season.get('index')
        out = {
            'index': index,
            'season': alt_season.get('season'),
            'outperformers': alt_season.get('outperformers'),
            'total_analyzed': alt_season.get('total_analyzed'),
            'color': _get_alt_season_color(index),
        }
        if index is None:
            out['error'] = alt_season.get('error') or 'unavailable'
        return out

    def _fetch_mvrv():
        mvrv = db.fetch_mvrv_zscore()
        return {
            'zscore': mvrv.get('zscore'),
            'zone': mvrv.get('zone', 'neutral'),
            'color': _get_mvrv_color(mvrv.get('zscore', 0)),
        }

    def _fetch_fr():
        fr = db.get_funding_rate('BTC')
        return {
            'rate': fr,
            'rate_pct': round(fr * 100, 4) if fr else None,
            'color': _get_funding_rate_color(fr),
        }

    def _fetch_oi():
        oi = db.fetch_open_interest('BTC')
        return {
            'value': oi.get('value'),
            'value_usd': oi.get('value_usd'),
            'change_pct': oi.get('change_pct'),
            'color': '#adb5bd',
        }

    def _fetch_regime():
        from carrac.sentiment.regime import get_or_compute_regime
        regime = get_or_compute_regime()
        if not regime:
            # get_or_compute_regime() returns None (never raises) on total
            # failure — raise here so the ThreadPoolExecutor task result
            # surfaces as an exception and the caller's existing
            # `except Exception` branch substitutes fallbacks['regime']
            # (previous behaviour, preserved).
            raise RuntimeError('Regime unavailable — cache miss and fresh compute failed')
        return regime

    def _fetch_fred():
        from data_sources.fred import get_fred_client
        return get_fred_client().get_all()

    fallbacks = {
        'fear_greed': {'value': None, 'classification': None, 'error': True},
        'long_short': {'error': True},
        'alt_season': {'index': None, 'season': None, 'error': True},
        'mvrv_zscore': {'zscore': None, 'zone': 'unknown', 'error': True},
        'funding_rate': {'rate': None, 'rate_pct': None, 'error': True},
        'open_interest': {'value': None, 'value_usd': None, 'change_pct': None, 'error': True},
        'regime': {'regime': 'N/A', 'score': 0, 'color': '#8b949e', 'components': []},
        'fred': {},
    }
    tasks = {
        'fear_greed': _fetch_fg,
        'long_short': _fetch_ls,
        'alt_season': _fetch_alt,
        'mvrv_zscore': _fetch_mvrv,
        'funding_rate': _fetch_fr,
        'open_interest': _fetch_oi,
        'regime': _fetch_regime,
        'fred': _fetch_fred,
    }

    sentiment = {}
    # NOT a `with` block. ThreadPoolExecutor.__exit__ calls
    # shutdown(wait=True), and future.result(timeout=...) gives up on a task
    # without cancelling it — so a `with` would re-join the very task the
    # timeout had just abandoned. A FRED outage therefore blocked the request
    # for the full retry budget (measured: 79.30s) despite a 30s guard.
    # shutdown(wait=False) lets the caller leave on the deadline; the orphaned
    # task drains on its own and, thanks to the now-shared circuit breaker,
    # short-circuits within seconds once the source is confirmed down.
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix='sentiment')
    try:
        futures = {key: executor.submit(fn) for key, fn in tasks.items()}
        for key, future in futures.items():
            remaining = max(0.0, deadline - time.monotonic())
            if key in _FRED_DEPENDENT_KEYS:
                remaining = min(remaining, fred_subbudget)
            try:
                sentiment[key] = future.result(timeout=remaining)
            except Exception as e:
                # `future.done()` is the exact discriminator: still running means
                # we ran out of wall clock, not that the task failed. Do NOT test
                # for TimeoutError here — since 3.10 `socket.timeout` IS builtin
                # TimeoutError, so a genuinely failing task would be misreported
                # as budget exhaustion and page a human for nothing.
                if not future.done():
                    _alarm_budget_exhausted(key)
                else:
                    logger.warning(f"Failed to fetch {key}: {e}")
                sentiment[key] = fallbacks[key]
    finally:
        executor.shutdown(wait=False)

    return {
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'sentiment': sentiment,
    }


def _refresh_sentiment_background():
    """Run a fresh sentiment compute in the background and update the cache.

    Clears the refreshing flag when done (success or failure).
    """
    from carrac.caches import set_cached_sentiment, set_sentiment_refreshing
    try:
        result = _compute_sentiment(budget_s=_SENTIMENT_BUDGET_BACKGROUND_S)
        try:
            set_cached_sentiment(result)
        except Exception as e:
            logger.warning(f"Background sentiment cache write failed: {e}")
        with _sentiment_response_lock:
            _sentiment_response_cache['data'] = (time.time(), result)
        logger.debug("Background sentiment refresh complete")
    except Exception as e:
        logger.warning(f"Background sentiment refresh failed: {e}")
    finally:
        set_sentiment_refreshing(False)


@sentiment_bp.route('/api/market-sentiment')
@auth_or_key_required
def api_market_sentiment():
    """Get aggregated market sentiment indicators."""
    from carrac.caches import (
        get_cached_sentiment,
        get_stale_sentiment,
        is_sentiment_refreshing,
        set_sentiment_refreshing,
        set_cached_sentiment,
    )

    # 1. Fresh cross-worker Redis cache hit — return immediately (<1 ms).
    shared = get_cached_sentiment()
    if shared:
        return jsonify(shared)

    # 2. In-process cache hit (same worker, sub-TTL) — return immediately.
    with _sentiment_response_lock:
        cached = _sentiment_response_cache.get('data')
        if cached and (time.time() - cached[0]) < _SENTIMENT_RESPONSE_TTL:
            return jsonify(cached[1])

    # 3. Stale-while-revalidate: if we have any previous result (even expired),
    #    return it immediately and trigger an async refresh so the next request
    #    gets fresh data.  This prevents every request in the revalidation
    #    window from blocking on upstream fetches.
    stale = get_stale_sentiment()
    if stale:
        # A refresh already in flight is the STRONGEST reason to
        # serve stale, not a reason to skip it. Gating on
        # is_sentiment_refreshing() would send every request arriving during a
        # slow refresh down the synchronous path below, multiplying one slow
        # FRED refresh into many slow requests. The flag gates only
        # whether we start a SECOND refresh.
        if not is_sentiment_refreshing():
            set_sentiment_refreshing(True)
            t = threading.Thread(
                target=_refresh_sentiment_background,
                daemon=True,
                name='SentimentRefresh',
            )
            t.start()
            logger.debug("Serving stale sentiment; background refresh triggered")
        else:
            logger.debug("Serving stale sentiment; refresh already in flight")
        return jsonify(stale)

    # 4. Absolute cold start (no cached data at all) — compute synchronously
    #    so the first user does not receive an empty/error response.
    try:
        result = _compute_sentiment()

        from carrac.formatters import maybe_compact, compact_sentiment

        # Populate caches for subsequent requests.
        try:
            set_cached_sentiment(result)
        except Exception as e:
            logger.warning(f"Failed to write shared sentiment cache: {e}")
        with _sentiment_response_lock:
            _sentiment_response_cache['data'] = (time.time(), result)

        return jsonify(maybe_compact(result, compact_sentiment))

    except Exception as e:
        logger.error(f"Error in market sentiment endpoint: {e}")
        return jsonify({'status': 'error', 'error': 'Failed to fetch sentiment data'}), 500


@sentiment_bp.route('/api/sentiment-history')
@auth_or_key_required
def api_sentiment_history():
    """Get sentiment history for the last N hours."""
    from carrac.db_pool import get_conn, adapt_query
    hours = request.args.get('hours', 24, type=int)
    hours = min(hours, 168)

    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            adapt_query('SELECT * FROM sentiment_history WHERE timestamp > ? ORDER BY timestamp ASC'),
            (cutoff,),
        )
        rows = cur.fetchall()

    return jsonify({
        'status': 'ok',
        'count': len(rows),
        'snapshots': [dict(r) for r in rows],
    })
