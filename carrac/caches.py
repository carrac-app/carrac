"""
Shared TTL caches for chart data, derivatives, volume profile, etc.

Redis-backed (shared across Gunicorn workers) with in-memory fallback.
Sub-hourly kline cache stays in-memory only (DataFrame, short TTL).
"""

import json
import logging
import math
import re
import time
import threading

import numpy as np

logger = logging.getLogger('carrac.caches')


def _get_redis():
    """Get Redis client (reuses singleton from redis_cache module)."""
    try:
        from carrac.redis_cache import get_redis
        return get_redis()
    except Exception:
        return None


def _json_default(obj):
    """JSON serializer for numpy types."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Not serializable: {type(obj)}")


# Conservative default for the in-process "fast path" window
# (see SharedTTLCache.get). Without it every Redis-path get() does a
# GET + json.loads on EVERY call, even for huge, slowly-rewritten blobs
# (e.g. the 10.8MB scanner payload, rewritten ~every 30min by precompute) —
# this collapses that to zero for most requests while bounding cross-worker
# staleness to `local_fast_ttl` seconds. Instances holding especially large
# or slow-changing payloads may override this at construction (see the
# per-instance comments below); everything else keeps this default.
_DEFAULT_LOCAL_FAST_TTL = 5


class SharedTTLCache:
    """TTL cache: Redis (cross-worker) with in-memory fallback.

    Redis keys use prefix 'mcd:c:{name}:{key}'.
    In-memory dict used as fallback if Redis is down.
    """

    def __init__(self, name, ttl, max_local=200, local_fast_ttl=_DEFAULT_LOCAL_FAST_TTL):
        self._prefix = f'mcd:c:{name}:'
        self._ttl = ttl
        self._max_local = max_local
        self._local = {}
        self._lock = threading.Lock()
        self._local_fast_ttl = local_fast_ttl

    def _prune_local_locked(self):
        """Evict local entries older than self._ttl. Caller must hold self._lock."""
        if len(self._local) > self._max_local:
            now = time.time()
            stale = [k for k, (t, _) in self._local.items() if now - t > self._ttl]
            for k in stale:
                del self._local[k]

    def get(self, key):
        # in-process fast path — if this worker's own copy is
        # younger than local_fast_ttl seconds, return it without touching
        # Redis at all.
        with self._lock:
            entry = self._local.get(key)
            if entry and (time.time() - entry[0]) < self._local_fast_ttl:
                return entry[1]
        r = _get_redis()
        if r:
            try:
                raw = r.get(self._prefix + key)
                if raw:
                    data = json.loads(raw)
                    # Re-arm the fast path with what Redis just returned so
                    # the next local_fast_ttl seconds of reads on this
                    # worker skip Redis entirely.
                    with self._lock:
                        self._local[key] = (time.time(), data)
                        self._prune_local_locked()
                    return data
            except Exception:
                pass
        # Redis miss/unavailable/erroring — fall back to the local copy
        # within the slower self._ttl window (unchanged from before the fast path).
        with self._lock:
            entry = self._local.get(key)
            if entry and (time.time() - entry[0]) < self._ttl:
                return entry[1]
        return None

    def set(self, key, data):
        r = _get_redis()
        if r:
            try:
                raw = json.dumps(data, default=_json_default)
                r.set(self._prefix + key, raw, ex=self._ttl + 30)
            except Exception as e:
                logger.debug(f"Redis cache set error ({self._prefix}{key}): {e}")
        # Always write local (fallback + faster reads for same worker)
        with self._lock:
            self._local[key] = (time.time(), data)
            self._prune_local_locked()


# ============================================================
# Derivatives Cache (OI, CVD Futures)
# ============================================================
_derivatives = SharedTTLCache('deriv', ttl=300, max_local=100)


def get_cached_derivatives(cache_key):
    """Get cached derivatives data if still fresh."""
    return _derivatives.get(cache_key)


def set_cached_derivatives(cache_key, data):
    """Cache derivatives data."""
    _derivatives.set(cache_key, data)


# ============================================================
# Volume Profile Cache
# ============================================================
_vp = SharedTTLCache('vp', ttl=300, max_local=50)


def get_cached_vp(cache_key):
    return _vp.get(cache_key)


def set_cached_vp(cache_key, data):
    _vp.set(cache_key, data)


# ============================================================
# Sentiment Response Cache (cross-worker — was in-process only,
# which left MCP market_get_sentiment failing ~75% of the time under
# 4 gunicorn workers)
# ============================================================
_sentiment = SharedTTLCache('sentiment', ttl=120, max_local=4)

# Stale-while-revalidate: serve data up to this many seconds past TTL while
# a background refresh is in-flight.  Kept well below the HTTP
# Cache-Control max-age (300s) so clients still receive fresh data regularly.
_SENTIMENT_STALE_TTL = 300   # serve stale for up to 5 min
_sentiment_refreshing = threading.Event()


def get_cached_sentiment():
    return _sentiment.get('data')


def get_stale_sentiment():
    """Return the last-good sentiment payload regardless of freshness.

    Used by the stale-while-revalidate path: the caller has already
    established that the fresh cache is empty and will trigger a background
    refresh.  Returns None only if there has never been a successful fetch.
    """
    # Check local store first (fastest, no network).
    with _sentiment._lock:
        entry = _sentiment._local.get('data')
        if entry:
            age = time.time() - entry[0]
            if age < _SENTIMENT_STALE_TTL:
                return entry[1]

    # Fall back to Redis with a relaxed TTL read via a raw GET.
    r = _get_redis()
    if r:
        try:
            raw = r.get(_sentiment._prefix + 'data')
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    return None


def is_sentiment_refreshing() -> bool:
    """True if a background refresh is already running in this process."""
    return _sentiment_refreshing.is_set()


def set_sentiment_refreshing(flag: bool) -> None:
    """Mark / clear the in-process background refresh flag."""
    if flag:
        _sentiment_refreshing.set()
    else:
        _sentiment_refreshing.clear()


def set_cached_sentiment(data):
    _sentiment.set('data', data)


# ============================================================
# Chart Response Cache — TF-aware TTLs + serve-stale-while-revalidate (SWR)
# ============================================================
_CHART_TTLS = {
    '1m': 30, '3m': 30, '5m': 45,
    '15m': 60, '30m': 60,
    # 1h/4h are 120s — headroom over the worker warm cadence (~60s; a TTL
    # near the cadence starves skipped_fresh). SWR stale-serving decouples
    # user latency from this constant entirely; its only effect is the number
    # of redundant rebuilds.
    '1h': 120, '4h': 120,
    '1D': 180, '3D': 300, '1W': 600, '1M': 900,
}
_CHART_DEFAULT_TTL = 90
# Redis retention for the chart cache, independent of the TF TTL
# above. Freshness is enforced by get_cached_chart from the payload's own
# `_cached_at` stamp; this just bounds how long a STALE copy survives for the
# SWR fallback before a lookup goes fully cold (returns None, forcing a
# synchronous rebuild).
CHART_SWR_RETENTION_S = 3600
# NOTE: get_cached_chart/set_cached_chart below bypass
# SharedTTLCache.get()/.set() entirely (they read/write `_chart._local` and
# `_chart._prefix` directly for their own SWR freshness logic) — so this
# instance's local_fast_ttl is inert. Left at the class default rather than
# removed, in case a future caller starts using .get()/.set() directly.
_chart = SharedTTLCache('chart', ttl=900, max_local=200)

# Intraday cache keys carry a trailing `:b<epoch_bucket>` suffix (see
# routes.py's `_cache_suffix` — always appended LAST, after `:col`/`:q...`)
# so a payload built in one candle period is never mistaken for the current
# one. Stripping it back to the bucket-agnostic alias is a trailing anchor.
_CHART_BUCKET_SUFFIX_RE = re.compile(r':b\d+$')

# Persistent ledger of user-facing blocking misses, keyed by day.
# The only other record of a miss is a docker-logs line in a 6h window that a
# deploy's container recreate wipes, which would leave miss-share readings
# with no per-pair evidence behind them.
# The warm-tier re-scope must be sized from what users actually request, so
# the counts have to survive the container: read the keys under this prefix
# to see which symbol/timeframe pairs miss the warm cache most often.
CHART_MISS_LEDGER_PREFIX = 'mcd:chartmiss:'
CHART_MISS_LEDGER_TTL_S = 8 * 86400  # 8 days: one weekly review window + slack


def record_chart_miss(symbol, tf, r=None, day=None):
    """Count one blocking miss for (symbol, tf) in today's ledger hash.

    Best-effort by design: this rides the already-slow cold path, and a
    Redis fault here must never cost the user their chart on top of the
    build they are waiting for. Redis being down is loudly visible from
    every cache path already — this debug line adds attribution, not the
    only signal.
    """
    try:
        if r is None:
            r = _get_redis()
        if day is None:
            day = time.strftime('%Y-%m-%d', time.gmtime())
        key = CHART_MISS_LEDGER_PREFIX + day
        pipe = r.pipeline()
        pipe.hincrby(key, f'{symbol}:{tf}', 1)
        pipe.expire(key, CHART_MISS_LEDGER_TTL_S)
        pipe.execute()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"chart-miss ledger write failed for {symbol}:{tf}: {e}")


def sanitize_nonfinite(obj):
    """Replace NaN/Infinity floats with None, recursively.

    Stdlib json.dumps emits literal ``NaN`` (invalid
    JSON) for non-finite floats, and a payload poisoned that way gets frozen
    into the chart cache and hard-crashes JSON.parse in every browser that
    loads it. Chart payloads pass through here before caching/serving so a
    transient bad value degrades to ``null`` (which the frontend already
    tolerates, e.g. clamped CPR supports) instead of breaking the chart.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_nonfinite(v) for v in obj]
    if isinstance(obj, np.floating):
        v = float(obj)
        return v if math.isfinite(v) else None
    return obj


def _chart_cache_key(symbol, tf, lite):
    return f"{symbol}:{tf}:{1 if lite else 0}"


def _chart_payload_age(data):
    """Age in seconds since a chart payload was stamped by set_cached_chart,
    or None if the payload predates SWR (no `_cached_at`) — callers treat
    that as stale rather than guessing an age."""
    cached_at = data.get('_cached_at') if isinstance(data, dict) else None
    if cached_at is None:
        return None
    return time.time() - cached_at


def get_cached_chart(symbol, tf, lite):
    """Get cached chart response, serve-stale-while-revalidate aware.

    Returns ``(payload, is_fresh)``, or ``None`` if nothing is cached (or the
    CHART_SWR_RETENTION_S window has fully lapsed everywhere).

    ``is_fresh`` requires the EXACT requested key (bucket included, if any)
    to be within its TF TTL. A bucketed key found only via its
    bucket-stripped alias (i.e. this is a new candle period and the exact
    key hasn't been rebuilt yet) is ALWAYS reported stale, regardless of the
    alias payload's own age — it may be missing the current forming candle.
    Legacy Redis payloads with no `_cached_at` (pre-SWR) are also stale.

    Local-dict entries need no `_cached_at` check — they're always written
    by set_cached_chart in this same process, so the tuple's own timestamp
    is authoritative.
    """
    # TTL lookup must use the BASE timeframe — the tf argument arrives with
    # cache-variant suffixes attached (':col', ':q...', ':b<n>'), and the
    # frontend always sends fmt=col, so looking up the suffixed string would
    # silently give every real request the 90s default instead of its per-TF TTL.
    ttl = _CHART_TTLS.get(tf.split(':', 1)[0], _CHART_DEFAULT_TTL)
    key = _chart_cache_key(symbol, tf, lite)
    now = time.time()

    # Exact key — local first (cheap, no network), then Redis.
    with _chart._lock:
        entry = _chart._local.get(key)
    if entry is not None:
        ts, data = entry
        age = now - ts
        if age < CHART_SWR_RETENTION_S:
            return data, age < ttl
        # else: past local retention — fall through, another worker may hold
        # a fresher copy in Redis.
    r = _get_redis()
    if r:
        try:
            raw = r.get(_chart._prefix + key)
            if raw:
                data = json.loads(raw)
                age = _chart_payload_age(data)
                return data, (age is not None and age < ttl)
        except Exception:
            pass

    # Exact key absent (or past retention) everywhere. Bucketed keys fall
    # back to the bucket-stripped alias and are served stale from there — the
    # retention window, not the TF TTL, is what bounds how old that is.
    alias_tf = _CHART_BUCKET_SUFFIX_RE.sub('', tf)
    if alias_tf == tf:
        return None
    alias_key = _chart_cache_key(symbol, alias_tf, lite)
    with _chart._lock:
        entry = _chart._local.get(alias_key)
    if entry is not None:
        ts, data = entry
        if now - ts < CHART_SWR_RETENTION_S:
            return data, False
    if r:
        try:
            raw = r.get(_chart._prefix + alias_key)
            if raw:
                return json.loads(raw), False
        except Exception:
            pass
    return None


def set_cached_chart(symbol, tf, lite, data):
    """Cache chart response, stamped for SWR freshness checks.

    Stamps `_cached_at` as a dict key INSIDE the payload (no envelope) so
    legacy readers ignoring it still get a valid payload back. Redis
    retention is CHART_SWR_RETENTION_S regardless of TF TTL — get_cached_chart
    enforces real freshness from `_cached_at`; the wider retention just bounds
    how long a stale copy survives for the SWR fallback.

    Bucketed intraday keys (`tf` ends in `:b<n>`) also get a bucket-stripped
    ALIAS write of the same payload, so the very next candle period's lookup
    (a fresh bucket — no entry yet under that exact key) can still serve this
    payload stale instead of blocking on a cold rebuild.
    """
    data['_cached_at'] = int(time.time())
    # Sanitize IN PLACE so callers that go on to serve this same dict
    # (Flask's own chart assembly) respond with the clean payload too.
    clean = sanitize_nonfinite(data)
    data.clear()
    data.update(clean)
    key = _chart_cache_key(symbol, tf, lite)
    now = time.time()
    raw = None
    r = _get_redis()
    if r:
        try:
            raw = json.dumps(data, default=_json_default)
            r.set(_chart._prefix + key, raw, ex=CHART_SWR_RETENTION_S)
        except Exception as e:
            logger.debug(f"Redis chart set error: {e}")
    with _chart._lock:
        _chart._local[key] = (now, data)
        if len(_chart._local) > _chart._max_local:
            stale = [k for k, (t, _) in _chart._local.items() if now - t > CHART_SWR_RETENTION_S]
            for k in stale:
                del _chart._local[k]

    alias_tf = _CHART_BUCKET_SUFFIX_RE.sub('', tf)
    if alias_tf != tf:
        alias_key = _chart_cache_key(symbol, alias_tf, lite)
        if r and raw is not None:
            try:
                r.set(_chart._prefix + alias_key, raw, ex=CHART_SWR_RETENTION_S)
            except Exception as e:
                logger.debug(f"Redis chart alias set error: {e}")
        with _chart._lock:
            _chart._local[alias_key] = (now, data)


# ============================================================
# Sub-hourly Kline DataFrame Cache (in-memory only — DataFrame)
# ============================================================
_subhourly_cache = {}
_subhourly_cache_lock = threading.Lock()
_SUBHOURLY_CACHE_TTL = 60


def get_cached_subhourly(symbol, interval):
    """Get cached sub-hourly DataFrame if still fresh."""
    key = f"{symbol}:{interval}"
    with _subhourly_cache_lock:
        entry = _subhourly_cache.get(key)
        if entry and (time.time() - entry[0]) < _SUBHOURLY_CACHE_TTL:
            return entry[1]
    return None


def set_cached_subhourly(symbol, interval, df):
    """Cache sub-hourly DataFrame."""
    key = f"{symbol}:{interval}"
    with _subhourly_cache_lock:
        _subhourly_cache[key] = (time.time(), df)


# ============================================================
# Relative Strength Cache
# ============================================================
_rs = SharedTTLCache('rs', ttl=900, max_local=5)


def get_cached_rs():
    """Get cached relative strength data if still fresh."""
    return _rs.get('data')


def set_cached_rs(data):
    """Cache relative strength data."""
    _rs.set('data', data)


# ============================================================
# Market Regime Cache
# ============================================================
_regime = SharedTTLCache('regime', ttl=300, max_local=5)


def get_cached_regime():
    """Get cached regime data if still fresh."""
    return _regime.get('data')


def set_cached_regime(data):
    """Cache regime data."""
    _regime.set('data', data)


# ============================================================
# Scanner Cache
# ============================================================
# This is the 10.8MB blob rewritten only ~every 30min by
# precompute — a much wider local_fast_ttl
# than the 5s default is safe here since cross-worker staleness of even 10s
# is negligible against a 30min rewrite cadence, and it's the read that most
# benefits from skipping the per-request Redis GET + json.loads.
_scanner = SharedTTLCache('scanner', ttl=3600, max_local=5, local_fast_ttl=10)


# last-known-good scanner snapshot, persisted with NO TTL. On a
# cold start (worker restart before the first precompute pass, or the 1h
# scanner TTL expiring without a refresh) the fresh key is empty and
# Scanner/Radar return 503 / empty for tens of minutes. We keep an untimed copy
# on every successful cache write and, for UI read paths ONLY (opt-in via
# ``allow_stale``), serve it STALE-MARKED so those surfaces are never blank.
# Alert/report/MCP evaluators keep the strict fresh-or-None behaviour (default
# ``allow_stale=False``) so nothing evaluates against stale data.
_SCANNER_LKG_KEY = 'mcd:c:scanner:lkg'
_scanner_lkg_lock = threading.Lock()
_scanner_lkg_local = None  # (stored_at, data) — fallback when Redis is down


def _copy_scanner_payload(data):
    """Defensive copy of a scanner payload: the top-level dict PLUS a fresh
    ``results`` list of fresh per-asset dicts (everything else — ``macro``,
    nested ``long``/``short``/``confluences`` sub-dicts — stays shared, no
    consumer touches those in place).

    A consumer may temporarily mutate a result dict and clean up before
    returning (see ``carrac/scanner/routes.py``'s ``direction_filter`` branch,
    which sets ``_filter_score``/``_filter_verdict`` on each result then
    ``.pop()``s them before the response is built). The fast path can hand the SAME object graph to many
    concurrent/sequential requests on a worker for up to
    ``local_fast_ttl`` seconds (gunicorn here runs UvicornWorker, so
    synchronous Flask routes execute in a thread pool — genuinely
    concurrent with each other), that mutate-then-pop pattern would
    otherwise race against other readers of the shared local copy. Callers
    of ``get_cached_scanner()`` are NOT in files we own, so the fix lives
    here rather than in ``scanner/routes.py``.
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)
    results = out.get('results')
    if isinstance(results, list):
        out['results'] = [dict(r) if isinstance(r, dict) else r for r in results]
    return out


def get_cached_scanner(allow_stale=False):
    """Get cached scanner data if still fresh.

    ``allow_stale=True`` (UI read paths only) falls back to the last-known-good
    snapshot when the fresh key is missing, returning a shallow copy marked
    ``_stale=True`` with ``_stale_age_sec`` so Scanner/Radar render (stale)
    instead of empty. The default keeps the strict fresh-or-None contract.

    Always returns a defensive copy (see ``_copy_scanner_payload``) — never
    the object backing the shared in-process fast-path cache.
    """
    data = _scanner.get('data')
    if data is not None:
        return _copy_scanner_payload(data)
    if not allow_stale:
        return None
    return _get_scanner_lkg()


def set_cached_scanner(data):
    """Cache scanner data (fresh TTL key + untimed last-known-good copy +
    per-symbol hash mirror for composite readers)."""
    _scanner.set('data', data)
    _store_scanner_lkg(data)
    set_cached_scanner_by_symbol(data)


def _store_scanner_lkg(data):
    """Persist the last-known-good scanner snapshot with no expiry."""
    global _scanner_lkg_local
    stored_at = time.time()
    r = _get_redis()
    if r:
        try:
            raw = json.dumps({'stored_at': stored_at, 'data': data},
                             default=_json_default)
            r.set(_SCANNER_LKG_KEY, raw)  # no `ex` → survives worker restarts
        except Exception as e:
            logger.debug(f"scanner LKG set error: {e}")
    with _scanner_lkg_lock:
        _scanner_lkg_local = (stored_at, data)


def _get_scanner_lkg():
    """Read the last-known-good snapshot, stale-marked. None if never stored."""
    r = _get_redis()
    if r:
        try:
            raw = r.get(_SCANNER_LKG_KEY)
            if raw:
                wrapped = json.loads(raw)
                return _mark_scanner_stale(wrapped.get('data'),
                                           wrapped.get('stored_at'))
        except Exception:
            pass
    with _scanner_lkg_lock:
        if _scanner_lkg_local is not None:
            stored_at, data = _scanner_lkg_local
            return _mark_scanner_stale(data, stored_at)
    return None


def _mark_scanner_stale(data, stored_at):
    """Return a defensive copy of scanner data flagged stale (age in seconds)."""
    out = _copy_scanner_payload(data)
    if not isinstance(out, dict):
        return out
    out['_stale'] = True
    if stored_at:
        out['_stale_age_sec'] = int(time.time() - stored_at)
    return out


# ============================================================
# Per-symbol Hash Mirrors
# ============================================================
# The scanner (10.8MB) and HTF (2.1MB) blobs above are rewritten only
# ~every 30min by precompute, but /api/batch's actual callers (a terminal
# client polling 4 asset groups every ~30s, around the clock) only ever need <=10 symbols
# out of the whole blob. Every call still paid a full Redis GET +
# json.loads of BOTH blobs regardless — the SharedTTLCache in-process fast
# path can't help because the 30s poll cadence outlives the
# fast path's window across 4 gunicorn workers (measured: 2.6s cold-worker
# vs 0.26s warm-worker for the identical call).
#
# Fix: mirror each blob into a Redis HASH keyed by symbol, written in the
# SAME pass as the blob (see set_cached_scanner above and precompute.py's
# two response_cache.set('htf', ...) call sites) so hash and blob can never
# diverge for long, then have composite readers HMGET only the symbols
# they need (carrac/composite/assemblers.py's _get_scanner_by_symbol /
# _get_htf_by_symbol) instead of pulling the whole blob.
#
# TTL is ~2x the writer's own refresh cadence (~1800s for both blobs — see
# _scanner's ttl=3600 above and redis_cache.py ResponseCache.REFRESH_INTERVAL
# =1800) so a hash entry survives one missed refresh cycle without going
# stale forever, while self-healing if precompute stalls for longer.
_SCANNER_BY_SYM_KEY = 'mcd:c:scanner:by_sym'
_HTF_BY_SYM_KEY = 'mcd:response:htf:by_sym'
_SCANNER_BY_SYM_TTL = 3600
_HTF_BY_SYM_TTL = 3600


def _set_by_symbol_hash(hash_key, items, symbol_fn, ttl):
    """Best-effort mirror of `items` into a Redis HASH keyed by symbol_fn(item).

    Overwrites the whole hash atomically (DELETE + HSET + EXPIRE in one
    pipeline — redis-py pipelines are transactional by default) so a
    shrinking universe never leaves stale symbol fields behind. Never
    raises — a failure here just means readers keep falling back to the
    whole-blob path, same as if the hash were never written.
    """
    r = _get_redis()
    if not r:
        return
    mapping = {}
    for item in items:
        sym = symbol_fn(item)
        if not sym:
            continue
        try:
            mapping[sym] = json.dumps(item, default=_json_default)
        except Exception as e:
            logger.debug(f"by-symbol hash serialize error ({hash_key}/{sym}): {e}")
    if not mapping:
        return
    try:
        pipe = r.pipeline()
        pipe.delete(hash_key)
        pipe.hset(hash_key, mapping=mapping)
        pipe.expire(hash_key, ttl)
        pipe.execute()
    except Exception as e:
        logger.debug(f"by-symbol hash write error ({hash_key}): {e}")


def _get_by_symbol_hash(hash_key, symbols):
    """HMGET `symbols` from a Redis HASH.

    Returns ``{symbol: parsed_json}`` for fields that were present, or
    ``None`` when the hash itself is unavailable (doesn't exist yet —
    deploy ordering, expired — or Redis is down/erroring). The distinction
    matters: an EXISTING hash that simply contains none of the
    requested symbols must return ``{}`` and be trusted — e.g. the TUI's
    stocks/commodities groups aren't in the crypto-only scanner universe,
    and treating that as "unpopulated" made every such call fall back to
    fetching the 10.8MB blob only to find nothing there either (2-3s
    wasted per call, ~half of all /api/batch traffic).
    """
    r = _get_redis()
    if not r or not symbols:
        return None
    try:
        pipe = r.pipeline()
        pipe.exists(hash_key)
        pipe.hmget(hash_key, list(symbols))
        _exists, raw_values = pipe.execute()
        if not _exists:
            return None
    except Exception as e:
        logger.debug(f"by-symbol hash HMGET error ({hash_key}): {e}")
        return None
    out = {}
    for sym, raw in zip(symbols, raw_values):
        if raw is None:
            continue
        try:
            out[sym] = json.loads(raw)
        except Exception:
            continue
    return out


def set_cached_scanner_by_symbol(scanner_data):
    """Mirror scanner_data['results'] into the per-symbol scanner hash
    (``mcd:c:scanner:by_sym``). Called from set_cached_scanner so the hash
    is always written in the same pass as the blob."""
    if not isinstance(scanner_data, dict):
        return
    results = scanner_data.get('results')
    if not isinstance(results, list):
        return
    _set_by_symbol_hash(_SCANNER_BY_SYM_KEY, results, lambda r: r.get('symbol'), _SCANNER_BY_SYM_TTL)


def get_scanner_by_symbol_hash(symbols):
    """HMGET the requested symbols from the per-symbol scanner hash."""
    return _get_by_symbol_hash(_SCANNER_BY_SYM_KEY, symbols)


def set_htf_by_symbol_hash(htf_data):
    """Mirror the HTF response bundle's assets into the per-symbol HTF hash
    (``mcd:response:htf:by_sym``).

    Lives here rather than redis_cache.py so ResponseCache stays untouched:
    this reaches the same Redis client via ``_get_redis()`` (same singleton as redis_cache.get_redis)
    and writes an independent key. precompute.py calls this right after
    each ``response_cache.set('htf', data)`` so blob and hash are written
    in the same pass and can never diverge for long.
    """
    if not isinstance(htf_data, dict):
        return
    assets = htf_data.get('assets')
    if not isinstance(assets, list):
        return
    _set_by_symbol_hash(_HTF_BY_SYM_KEY, assets, lambda a: a.get('symbol'), _HTF_BY_SYM_TTL)


def get_htf_by_symbol_hash(symbols):
    """HMGET the requested symbols from the per-symbol HTF hash."""
    return _get_by_symbol_hash(_HTF_BY_SYM_KEY, symbols)
