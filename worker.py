"""
Carrac Compute Worker — background data processing.

Runs independently of the web server (gunicorn). Handles:
- HTF precompute loop (scanner scoring, alerts, transitions, plays)
- Cache refresh (ticker prices, macro data)
- Derivatives pre-warming (OI, CVD)
- Sentiment recording (hourly)
- Backfill (history gap-filling)
- Email digest flush

Communication with the web server is via Redis (response_cache). Services
(db, calculators, scorers) are built once via init_services() and shared
through the ServiceContainer module fallback.
"""

import logging
import os
import random
import signal
import threading
import time

# Defensive — docker-compose.yml already sets this. Ensures no surprise
# background-thread bootstrap inside create_app() if the worker ever imports
# the Flask app by accident.
os.environ['WORKER_MODE'] = 'true'


# Importing carrac at this point triggers Sentry/PostHog/logging bootstrap.
# Must happen before any logger.getLogger() call below so log formatting is
# consistent with the rest of the stack.
import carrac  # noqa: F401 — imported for module-load side effects

# Surface tag so the error tracker can distinguish worker crashes from web errors.
# Sentry is already initialised by the carrac package import above; we just
# layer in the per-process context.  If SENTRY_DSN is absent the block is a
# no-op (sentry_sdk stubs everything out when uninitialised).
import sentry_sdk as _sentry_sdk
_sentry_sdk.set_tag('surface', 'worker')

logger = logging.getLogger('carrac.worker')

# Build the ServiceContainer (db, calculators, scorers). This is the same
# path create_app() takes via install_on_app() — ends up populating the
# module-level fallback in carrac.services, so get_services() works in
# worker context without a Flask app.
from carrac.services import init_services, get_services
init_services()

from carrac.precompute import (
    warmup_cache,
    _precompute_loop,
    _stop_precompute,
    _stop_sentiment,
    _start_sentiment_scheduler,
)
from cache_manager import cache_manager, initialize_cache
import push_notifications

db = get_services().db


# ── CRM reconciliation scheduler ─────────────────────────
# Weekly EspoCRM <-> app-user reconciliation as a background thread — same
# pattern as _start_sentiment_scheduler above, NOT a crontab entry (production
# crontab edits are classifier-blocked).
_stop_crm_reconcile = threading.Event()

CRM_RECONCILE_INTERVAL_DAYS = 7
CRM_RECONCILE_REDIS_KEY = 'crm_reconcile_last'
# In-process fallback so the scheduler still runs at least once per worker
# lifetime even if Redis is unavailable (the shared Redis timestamp is what
# actually keeps the 7-day cadence across worker restarts/gunicorn reloads).
_crm_reconcile_last_run_fallback = {'ts': 0.0}


def _crm_reconcile_due() -> bool:
    """True when it has been >= CRM_RECONCILE_INTERVAL_DAYS since the last
    successful reconciliation run."""
    now = time.time()
    try:
        from carrac.caches import _get_redis
        r = _get_redis()
        if r:
            last = r.get(CRM_RECONCILE_REDIS_KEY)
            if not last:
                return True
            last_ts = float(last)
            return (now - last_ts) >= CRM_RECONCILE_INTERVAL_DAYS * 86400
    except Exception:
        pass
    return (now - _crm_reconcile_last_run_fallback['ts']) >= CRM_RECONCILE_INTERVAL_DAYS * 86400


def _crm_reconcile_mark_done() -> None:
    now = time.time()
    _crm_reconcile_last_run_fallback['ts'] = now
    try:
        from carrac.caches import _get_redis
        r = _get_redis()
        if r:
            r.set(CRM_RECONCILE_REDIS_KEY, str(now))
    except Exception:
        pass


def _start_crm_reconcile_scheduler():
    """Hourly wake; only actually runs run_reconciliation() when the 7-day
    cadence is due. Daemon thread — mirrors _start_sentiment_scheduler.

    EspoCRM is retired in production — ESPOCRM_URL
    / ESPOCRM_API_KEY are meant to be empty there. `_espocrm_request` no-ops
    (returns None) when unconfigured rather than raising, so a run that
    shouldn't happen at all would look like 4 separate "TargetList not
    found" errors (one per list) instead of the single "not configured" fact
    it actually is. Don't even schedule the thread when the CRM isn't
    configured — log one info line instead and return None.
    """
    from carrac.config import Config
    if not Config.ESPOCRM_URL or not Config.ESPOCRM_API_KEY:
        logger.info("CRM reconcile scheduler disabled (EspoCRM not configured)")
        return None

    def _loop():
        _stop_crm_reconcile.wait(random.randint(0, 300))
        while not _stop_crm_reconcile.is_set():
            try:
                if _crm_reconcile_due():
                    from carrac.crm_reconcile import run_reconciliation
                    summary = run_reconciliation()
                    # An aborted run (user_db outage) must NOT stamp the 7-day
                    # marker — retry on the next hourly wake instead.
                    if not summary.get('aborted'):
                        _crm_reconcile_mark_done()
            except Exception as e:
                logger.error(f"CRM reconcile scheduler run failed: {e}")
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception(e)
                except Exception:
                    pass
            _stop_crm_reconcile.wait(3600)

    t = threading.Thread(target=_loop, name='crm-reconcile-scheduler', daemon=True)
    t.start()
    return t


# ── Exchange auto-sync scheduler ───────────────────────────────
# Journal auto-sync from Binance/Hyperliquid — every 15 minutes, one pass over
# ALL users' connections. Mirrors _start_sentiment_scheduler's shape (jittered
# start wait + fixed-interval Event.wait loop). Per-connection error isolation
# and the paid-tier gate both live in carrac.journal.exchange_sync.
_stop_exchange_sync = threading.Event()
EXCHANGE_SYNC_INTERVAL_SECONDS = 900  # 15 minutes


def _start_exchange_sync_scheduler():
    """Daemon thread: every 15 minutes, sync every eligible exchange connection."""
    def _loop():
        _stop_exchange_sync.wait(random.randint(0, 60))
        while not _stop_exchange_sync.is_set():
            try:
                from carrac.journal.exchange_sync import sync_all_connections
                sync_all_connections()
            except Exception as e:
                logger.error(f"Exchange sync scheduler run failed: {e}")
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception(e)
                except Exception:
                    pass
            _stop_exchange_sync.wait(EXCHANGE_SYNC_INTERVAL_SECONDS)

    t = threading.Thread(target=_loop, name='exchange-sync-scheduler', daemon=True)
    t.start()
    logger.info("Exchange auto-sync scheduler started (every 15 min)")
    return t


# ── MCP intent-log purge + report-briefing retention scheduler ─────────
#
# Nightly 90-day retention purge of mcp_intent_log AND of reports.source_
# briefing — background thread, NOT a crontab entry (production crontab edits are
# classifier-blocked). Same shape as
# _start_crm_reconcile_scheduler above: hourly wake, only actually runs when
# the daily cadence is due. source_briefing retention deliberately REUSES this existing
# daily-cadence wake rather than adding a
# second scheduled job for what is the same "prune something old, once a
# day" shape.
_stop_intent_purge = threading.Event()

INTENT_PURGE_INTERVAL_DAYS = 1
INTENT_PURGE_REDIS_KEY = 'mcp_intent_purge_last'
_intent_purge_last_run_fallback = {'ts': 0.0}


def _intent_purge_due() -> bool:
    """True when it has been >= INTENT_PURGE_INTERVAL_DAYS since the last
    successful purge run."""
    now = time.time()
    try:
        from carrac.caches import _get_redis
        r = _get_redis()
        if r:
            last = r.get(INTENT_PURGE_REDIS_KEY)
            if not last:
                return True
            last_ts = float(last)
            return (now - last_ts) >= INTENT_PURGE_INTERVAL_DAYS * 86400
    except Exception:
        pass
    return (now - _intent_purge_last_run_fallback['ts']) >= INTENT_PURGE_INTERVAL_DAYS * 86400


def _intent_purge_mark_done() -> None:
    now = time.time()
    _intent_purge_last_run_fallback['ts'] = now
    try:
        from carrac.caches import _get_redis
        r = _get_redis()
        if r:
            r.set(INTENT_PURGE_REDIS_KEY, str(now))
    except Exception:
        pass


def _run_daily_retention():
    """Run every daily retention prune, each isolated from the others.

    Extracted from the scheduler loop so the set of prunes is
    testable without starting a thread or waiting on the daily cadence — the
    defect that prompted it was a prune nobody could see was not running.

    Each prune is independent: one failing must not skip the rest, and each
    failure is logged at error and beaconed, never swallowed.
    """
    def _run(label, fn):
        try:
            fn()
        except Exception as e:
            logger.error(f"{label} prune run failed: {e}")
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(e)
            except Exception:
                pass

    from carrac.mcp.runtime import purge_old_intent_log
    _run('mcp.intent_log', purge_old_intent_log)

    def _briefings():
        from carrac.content_db import get_content_db
        get_content_db().prune_old_source_briefings()
    _run('reports.source_briefing', _briefings)

    # ai_conversations claimed a 90-day retention that was never
    # actually enforced — its only caller was a PROBABILISTIC prune fired on
    # ~1% of new conversation writes, inside `except Exception: pass`. On a
    # product with almost no AI-chat traffic that effectively never ran, and
    # production carried rows 155 days old against the stated 90. Retention that
    # depends on write volume is not a retention period, so it lives here now
    # with the other daily prunes.
    def _conversations():
        from user_db import user_db
        user_db.prune_old_conversations(90)
    _run('ai_conversations', _conversations)


def _start_intent_purge_scheduler():
    """Hourly wake; only actually runs when the daily cadence is due, at
    which point it runs every prune in _run_daily_retention() — independent
    daily retention prunes sharing one thread/due-check/redis marker rather
    than one scheduler each.
    Daemon thread — mirrors _start_crm_reconcile_scheduler."""
    def _loop():
        _stop_intent_purge.wait(random.randint(0, 300))
        while not _stop_intent_purge.is_set():
            try:
                if _intent_purge_due():
                    _run_daily_retention()
                    _intent_purge_mark_done()
            except Exception as e:
                logger.error(f"MCP intent-log purge scheduler run failed: {e}")
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception(e)
                except Exception:
                    pass
            _stop_intent_purge.wait(3600)

    t = threading.Thread(target=_loop, name='mcp-intent-purge-scheduler', daemon=True)
    t.start()
    return t


def main():
    logger.info("Carrac compute worker starting...")

    # Cache manager must be initialised before background refresh.
    initialize_cache(start_refresh=False)
    cache_manager.start_background_refresh(interval=30)
    logger.info("Cache background refresh started")

    # Start backfiller
    try:
        from backfill import HistoryBackfiller
        backfiller = HistoryBackfiller(db)
        backfiller.start()
        logger.info("History backfiller started")
    except Exception as e:
        logger.warning(f"Backfiller failed to start: {e}")
        backfiller = None

    # Sentiment scheduler (hourly)
    _start_sentiment_scheduler()

    # CRM reconciliation scheduler (D-RECON — hourly wake, weekly cadence)
    _start_crm_reconcile_scheduler()

    # MCP intent-log purge + report source_briefing retention scheduler
    # (hourly wake, daily cadence)
    _start_intent_purge_scheduler()

    # Liquidation heatmap scheduler (every 15 min)
    try:
        from carrac.liq_heatmap import _start_liq_heatmap_scheduler
        _start_liq_heatmap_scheduler()
    except Exception as e:
        logger.warning(f"Liquidation heatmap scheduler failed to start: {e}")
    # Exchange auto-sync scheduler (every 15 min)
    _start_exchange_sync_scheduler()

    # Hyperliquid WS worker (if enabled)
    hl_ws_worker = None
    if os.environ.get('HYPERLIQUID_WS_ENABLED', '').lower() in ('1', 'true'):
        try:
            from data_sources.hyperliquid_ws import HyperliquidWSWorker
            from data_sources.hyperliquid import HyperliquidClient
            hl_ws_worker = HyperliquidWSWorker(
                csv_cache=db.cache,
                hl_coins=HyperliquidClient.HL_COINS,
                k_prefix_translation=HyperliquidClient.K_PREFIX_TRANSLATION,
            )
            hl_ws_worker.start()
            logger.info(f"HL WS worker started ({len(HyperliquidClient.HL_COINS)} coins)")
        except Exception as e:
            logger.warning(f"HL WS worker failed: {e}")

    # CCXT WS worker (if enabled)
    ccxt_ws_worker = None
    if os.environ.get('CCXT_WS_ENABLED', '').lower() in ('1', 'true'):
        try:
            from data_sources.ccxt_ws import CcxtWSWorker
            ccxt_ws_worker = CcxtWSWorker(csv_cache=db.cache, ccxt_client=db.ccxt)
            ccxt_ws_worker.start()
            logger.info("CCXT WS worker started")
        except Exception as e:
            logger.warning(f"CCXT WS worker failed: {e}")

    # Finnhub WS — broker between Finnhub's stream and Redis.
    finnhub_ws_worker = None
    try:
        from data_sources.finnhub_ws import FinnhubWSWorker
        finnhub_key = os.environ.get('FINNHUB_API_KEY', '').strip()
        if finnhub_key:
            finnhub_ws_worker = FinnhubWSWorker(api_key=finnhub_key)
            finnhub_ws_worker.start()
    except Exception as e:
        logger.warning(f"Finnhub WS worker failed: {e}")

    # Binance forced-liquidation WS — public, no auth. Default
    # ON; set LIQ_WS_ENABLED=0/false/no to disable. Failures here must never
    # take down the worker process.
    liq_ws_worker = None
    if os.environ.get('LIQ_WS_ENABLED', '1').lower() not in ('0', 'false', 'no'):
        try:
            from data_sources.binance_liq_ws import BinanceLiqWSWorker
            liq_ws_worker = BinanceLiqWSWorker()
            liq_ws_worker.start()
            logger.info("Binance Liq WS worker started")
        except Exception as e:
            logger.warning(f"Binance Liq WS worker failed: {e}")
    else:
        logger.info("Binance Liq WS worker skipped — LIQ_WS_ENABLED disabled")

    # HTF warmup so the web server has data immediately.
    logger.info("Starting HTF warmup...")
    warmup_cache()

    _write_heartbeat()

    logger.info("Entering precompute loop...")
    try:
        _precompute_loop()
    except KeyboardInterrupt:
        logger.info("Worker interrupted")
    finally:
        _shutdown(backfiller, hl_ws_worker, ccxt_ws_worker, finnhub_ws_worker, liq_ws_worker)


def _write_heartbeat():
    """Write worker heartbeat to Redis for health monitoring."""
    from carrac.caches import _get_redis
    r = _get_redis()
    if r:
        try:
            import json
            from datetime import datetime
            r.set('mcd:worker:heartbeat', json.dumps({
                'ts': datetime.now().isoformat(),
                'pid': os.getpid(),
            }), ex=300)
        except Exception:
            pass


def _shutdown(backfiller, hl_ws_worker, ccxt_ws_worker, finnhub_ws_worker=None, liq_ws_worker=None):
    """Graceful shutdown."""
    logger.info("Shutting down worker...")
    _stop_precompute.set()
    _stop_sentiment.set()
    _stop_crm_reconcile.set()
    _stop_intent_purge.set()
    try:
        from carrac.liq_heatmap import _stop_liq_heatmap
        _stop_liq_heatmap.set()
    except Exception:
        pass
    _stop_exchange_sync.set()
    if backfiller:
        backfiller.stop()
    if hl_ws_worker:
        hl_ws_worker.stop()
    if ccxt_ws_worker:
        ccxt_ws_worker.stop()
    if finnhub_ws_worker:
        finnhub_ws_worker.stop()
    if liq_ws_worker:
        liq_ws_worker.stop()
    cache_manager.stop_background_refresh()
    try:
        push_notifications.flush_email_digests()
    except Exception:
        pass
    logger.info("Worker shutdown complete")


def _sigterm_handler(signum, frame):
    logger.info("Received SIGTERM")
    _stop_precompute.set()
    _stop_sentiment.set()
    _stop_crm_reconcile.set()
    _stop_intent_purge.set()
    try:
        from carrac.liq_heatmap import _stop_liq_heatmap
        _stop_liq_heatmap.set()
    except Exception:
        pass
    _stop_exchange_sync.set()


signal.signal(signal.SIGTERM, _sigterm_handler)

if __name__ == '__main__':
    main()
