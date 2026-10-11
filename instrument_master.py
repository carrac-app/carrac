"""
Instrument master — reference-data layer (foundation phase).

Carrac holds hundreds of ticker strings and CSV caches (462 caches in production,
measured at the time of writing) and, until this module,
had no answer to "is this symbol still a real, traded instrument?" — every
guard built so far (Checks A-G in scripts/cache_integrity_check.py,
carrac/staleness.py's synthetic-bar rejection) is a symptom-catcher for that
missing question. The design decisions are recorded below and implemented here.

WHY THIS FILE SITS AT REPO ROOT, NOT IN carrac/
-------------------------------------------------
scripts/cache_integrity_check.py runs on the bare HOST as a cron, stdlib
only, specifically so it still works when the app container is down (see
that file's own docstring). It therefore never imports anything from the
``carrac`` package or ``csv_cache.py`` — both pull in pandas, and
``carrac/__init__.py`` additionally runs Sentry/PostHog bootstrap as an
import-time side effect, which has no business running on a bare-host cron.
This module follows the same discipline as ``csv_cache.py`` (repo root) and
goes one step further: it imports NOTHING beyond the stdlib, anywhere, at
module scope, so ``from instrument_master import load_store`` is safe from
the cron with zero new dependencies. Anything that legitimately needs a
network call (the Binance status ingestion helper) still only touches
``urllib.request`` — see ``fetch_binance_exchange_info``.

DESIGN DECISIONS (recorded here, not re-derived)
-------------------------------------------------------------------
1. Internal immutable id as the key; ticker is an attribute.
   The id is the dot-mangled cache-file stem (see ``_cache_stem`` — mirrors
   ``csv_cache.CSVCache.get_path``) so BTC.D and a
   hypothetical BTC_D never collide and never silently alias.
2. Per-status ``first_observed`` / ``last_confirmed`` knowledge dates
   — not a full bitemporal model, just two timestamps that
   answer "when did we start believing this status", separate from "when
   did the status become true".
3. Status vocabulary ``trading | suspended | delisted | unknown``, with
   ``unknown`` as the honest default. Never inferred as
   ``trading`` by default.
4. Status gates PUBLISHING, never RETENTION — this module
   never deletes a bar or a record. Reconciliation only ever adds/updates
   records; nothing here prunes cache files or rows.
5. Reconciliation is FILES-vs-MANIFEST, not
   manifest-only iteration — every ``*_daily.csv`` on disk gets a record
   (``tracking_state='orphaned'`` if untracked) and every manifest entry
   with no file gets one too (``tracking_state='reverse_orphan'``),
   matching the measured 28-orphan / 4-reverse-orphan split.
6. ``authoritative_source`` populated from what is
   mechanically derivable from the existing routing maps in
   ``data_sources/stooq.py`` (``WRONG_SCALE_ON_STOOQ``), ``data_sources/
   binance.py`` (``BinanceClient.UNAVAILABLE``), ``data_sources/
   hyperliquid.py`` (``HyperliquidClient.HL_COINS``) and ``database.py``
   (``crypto_map`` / ``crypto_metric_map``) — see
   ``derive_authoritative_source`` for exactly how, and why these are
   point-in-time COPIES rather than live imports.

WIRING
-------------------------------------------------------
Writer: ``carrac/precompute.py::_write_instrument_master`` runs once per
precompute cycle — reconcile, Binance status ingestion, lifecycle overrides,
delist candidates, then ``write_store``. Readers: ``csv_cache.py`` and
``carrac/precompute.py`` (``is_delisted`` / ``is_derived_series`` gates),
``carrac/staleness.py`` (``lifecycle_override_for``) and
``scripts/cache_integrity_check.py`` (``load_store`` / ``load_records``).

The import direction is one-way: this module must never import ``carrac``,
because the bare-host integrity cron imports it and that package's
``__init__`` bootstraps Sentry/PostHog. App code importing it is fine.
"""

import csv
import json
import os
import tempfile
from dataclasses import dataclass, fields, replace
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, NamedTuple, Optional

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Minimum vocabulary, 'unknown' as the honest default.
# Binance's exchangeInfo distinguishes TRADING from BREAK (temporary halt) —
# neither means delisted, so a boolean alive/dead column would be wrong
# within a month. 'delisted' is populated by something that can positively
# assert it (not by absence from a single feed's snapshot); nothing in this
# foundation phase sets it yet.
STATUSES = ('trading', 'suspended', 'delisted', 'unknown')
DEFAULT_STATUS = 'unknown'

# The files-vs-manifest reconciliation outcome for
# each id. 'tracked' = file exists and is in _tracked_symbols.json.
# 'orphaned' = file exists, no manifest entry (28 measured on real data at the time of writing).
# 'reverse_orphan' = manifest entry, no file (4 measured: BUX, HSTECH, RHM, SQ).
TRACKING_STATES = ('tracked', 'orphaned', 'reverse_orphan')

STORE_FILENAME = '_instrument_master.json'
TRACKED_MANIFEST_FILENAME = '_tracked_symbols.json'  # carrac/precompute.py::_write_tracked_symbols_manifest
DAILY_SUFFIX = '_daily.csv'


def _data_dir(data_dir: Optional[str] = None) -> Path:
    return Path(data_dir or os.environ.get('DATA_DIR', 'data'))


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).isoformat(timespec='seconds')


def _cache_stem(ticker: str) -> str:
    """The on-disk cache-file stem for ``ticker``, canonicalised the same way
    ``csv_cache.CSVCache.get_path`` does:
    ``symbol.upper().replace('.', '_').replace('^', '')``.

    This avoids false orphans: BTC.D,
    OTHERS.D and USDT.D read as orphans under a naive files-vs-manifest
    string comparison purely because the manifest keeps the dotted ticker
    while the cache filename mangles it. Reconciling on THIS value instead
    of the raw ticker is what makes BTC.D and BTC_D (if it ever existed)
    distinguishable, and BTC.D-vs-its-own-file NOT look like two different
    things.

    Duplicated rather than imported from csv_cache.py: that module imports
    pandas at module scope, and this module must stay pandas-free (see the
    module docstring — the stdlib-only cron needs this file to be safely
    importable with zero new dependencies).
    """
    return ticker.upper().replace('.', '_').replace('^', '')


# ---------------------------------------------------------------------------
# Record schema
# ---------------------------------------------------------------------------

@dataclass
class InstrumentRecord:
    """One row of the instrument master.

    ``id`` and ``cache_stem`` are equal today — the dot-mangled stem doubles
    as the internal immutable id (the ticker must not
    be the primary key). Kept as two fields, not one, so a future numeric or
    UUID id can be introduced without a schema break; ``cache_stem`` would
    stay the join key to the filesystem regardless of what ``id`` becomes.

    ``venue`` and ``authoritative_source`` are also equal today for every
    record this foundation phase can populate: Carrac's existing routing
    maps are feed-level ('binance', 'stooq', ...), not exchange-level
    ('NASDAQ', 'Binance Spot BTCUSDT', ...), so there is no finer venue
    granularity to record yet. Kept separate because
    the two are distinct concepts, and because a future collector could legitimately narrow ``venue`` (e.g. a
    specific trading pair) while ``authoritative_source`` stays the feed
    name.
    """
    id: str
    ticker: str
    cache_stem: str
    status: str = DEFAULT_STATUS
    tracking_state: str = 'orphaned'
    venue: str = 'unknown'
    authoritative_source: str = 'unknown'
    first_bar_date: Optional[str] = None
    last_bar_date: Optional[str] = None
    row_count: Optional[int] = None
    # Knowledge dates — when we first started
    # believing the CURRENT `status` value, and when we last re-confirmed it.
    first_observed: Optional[str] = None
    last_confirmed: Optional[str] = None
    # Venue-scoped listing status, kept SEPARATE from `status` on purpose.
    #
    # `status` is the instrument-level answer: is this thing still a real,
    # traded instrument anywhere. `binance_status` is only what Binance says
    # about the Binance pair, and the two genuinely disagree. Measured on
    # production: XMR, BEAM, LIT, RDNT and AERO are all BREAK/absent on
    # Binance while trading actively elsewhere with real varied OHLC and
    # volume (XMR was at $413 with 95k daily volume when measured).
    # Collapsing the venue's verdict into `status` would have marked five
    # live assets dead, which is the exact failure mode this module exists
    # to remove (an inference dressed up as a fact).
    #
    # Named for the venue rather than a generic `venue_status` so provenance
    # survives: if a second venue ever reports status, it gets its own field
    # or this becomes a per-venue mapping — it must not silently accumulate
    # statuses from different sources behind one name.
    binance_status: str = DEFAULT_STATUS
    binance_status_at: Optional[str] = None
    # When this record FIRST met the delisting-candidate conjunction
    # (``delist_candidates`` below). Not a status and never read as one: it
    # exists so the proposer's alarm is edge-triggered — fires once when a
    # symbol becomes a candidate, silent on every cycle it merely stays one.
    # Cleared the moment it stops qualifying, so a symbol that goes quiet,
    # recovers and dies again is reported both times. None = not a candidate.
    delist_candidate_since: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'ticker': self.ticker,
            'cache_stem': self.cache_stem,
            'status': self.status,
            'tracking_state': self.tracking_state,
            'venue': self.venue,
            'authoritative_source': self.authoritative_source,
            'first_bar_date': self.first_bar_date,
            'last_bar_date': self.last_bar_date,
            'row_count': self.row_count,
            'first_observed': self.first_observed,
            'last_confirmed': self.last_confirmed,
            'binance_status': self.binance_status,
            'binance_status_at': self.binance_status_at,
            'delist_candidate_since': self.delist_candidate_since,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> 'InstrumentRecord':
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (payload or {}).items() if k in valid})


_RECORD_FIELDS = {f.name for f in fields(InstrumentRecord)}


# ---------------------------------------------------------------------------
# Atomic store — writer mirrors CSVCache._atomic_write_csv's temp+os.replace
# idiom (csv_cache.py::_record_weekend_bar_breadcrumb): a truncate-then-write would leave
# BTC_daily.csv readable mid-write. Same shape:
# write to a temp file in the SAME directory, fsync, then os.replace — a
# reader that opens this file concurrently (the stdlib cron) always sees
# either the whole old file or the whole new one, never a mixture.
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def store_path(data_dir: Optional[str] = None) -> Path:
    return _data_dir(data_dir) / STORE_FILENAME


def load_store(data_dir: Optional[str] = None) -> dict:
    """Stdlib-only read of the instrument master JSON file.

    This is the function ``scripts/cache_integrity_check.py`` needs
    to be able to call without pandas. Never raises: a missing or corrupt
    store degrades to an empty one, matching
    ``scripts/cache_integrity_check.py::load_tracked_symbols``'s posture of
    "cannot judge" over "guess".
    """
    path = store_path(data_dir)
    try:
        with open(path) as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {'instruments': {}, 'written_at': None}
    if not isinstance(payload, dict) or not isinstance(payload.get('instruments'), dict):
        return {'instruments': {}, 'written_at': None}
    return payload


def load_records(data_dir: Optional[str] = None) -> Dict[str, InstrumentRecord]:
    """``load_store`` decoded into ``InstrumentRecord`` objects, keyed by id."""
    payload = load_store(data_dir)
    out = {}
    for rid, raw in payload.get('instruments', {}).items():
        try:
            out[rid] = InstrumentRecord.from_dict(raw)
        except TypeError:
            continue  # corrupt individual record — skip it, don't fail the load
    return out


def write_store(records: Dict[str, InstrumentRecord], data_dir: Optional[str] = None,
                 now: Optional[datetime] = None) -> Path:
    """Atomically write ``records`` (id -> InstrumentRecord) to the store."""
    path = store_path(data_dir)
    payload = {
        'instruments': {rid: rec.to_dict() for rid, rec in records.items()},
        'written_at': _now_iso(now),
    }
    _atomic_write_json(path, payload)
    return path


# ---------------------------------------------------------------------------
# Tracked-symbols manifest (own minimal reader — see module docstring for why
# this is not imported from scripts/cache_integrity_check.py: that file is
# explicitly out of scope for the foundation phase, and importing it
# would also pull in its ntfy/urllib module-level state).
# ---------------------------------------------------------------------------

def read_tracked_manifest(data_dir: Optional[str] = None) -> set:
    """The tickers currently in ``_tracked_symbols.json``, upper-cased.

    Returns an empty set on any read failure — reconciliation then treats
    every cache file as orphaned, which is the safe direction (an unexamined file must never be silently called "clean").
    """
    path = _data_dir(data_dir) / TRACKED_MANIFEST_FILENAME
    try:
        with open(path) as fh:
            payload = json.load(fh)
        symbols = payload.get('symbols') or []
        return {str(s).upper() for s in symbols}
    except (OSError, ValueError, AttributeError):
        return set()


# ---------------------------------------------------------------------------
# Cache-dir scan — reads the date column positionally, exactly like
# scripts/cache_integrity_check.py::scan() does, for the same reason: the
# index header varies (crypto frames name it "timestamp", stock/forex/index
# frames leave it blank) but the date is always column 0.
# ---------------------------------------------------------------------------

def list_cache_files(cache_dir) -> Dict[str, Path]:
    """cache-file stem -> Path, for every ``*_daily.csv`` in ``cache_dir``."""
    cache_dir = Path(cache_dir)
    out: Dict[str, Path] = {}
    if not cache_dir.is_dir():
        return out
    for name in sorted(os.listdir(cache_dir)):
        if not name.endswith(DAILY_SUFFIX):
            continue
        out[name[: -len(DAILY_SUFFIX)]] = cache_dir / name
    return out


def _scan_cache_file(path: Path):
    """Return (first_date_iso, last_date_iso, row_count) for one cache file,
    reading positionally so this never needs pandas. Unreadable/empty file
    yields (None, None, 0), never raises.
    """
    dates = []
    try:
        with open(path, newline='') as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            for row in reader:
                if not row:
                    continue
                raw = row[0][:10]
                if len(raw) != 10:
                    continue
                try:
                    dates.append(date.fromisoformat(raw))
                except ValueError:
                    pass
    except OSError:
        return (None, None, 0)
    if not dates:
        return (None, None, 0)
    dates.sort()
    return (dates[0].isoformat(), dates[-1].isoformat(), len(dates))


# ---------------------------------------------------------------------------
# authoritative_source derivation
#
# Static, point-in-time COPIES of the routing decisions already made in
# data_sources/stooq.py, data_sources/binance.py, data_sources/hyperliquid.py
# and database.py. Copied rather than imported live,
# for two independent reasons:
#
#   1. Every one of data_sources/{stooq,binance,hyperliquid}.py imports
#      pandas at module scope (transitively, via data_sources/base.py in
#      binance/hyperliquid's case). This module must not import pandas at
#      module scope — see the module docstring.
#   2. database.py's crypto_map / crypto_metric_map are built inside a class
#      __init__ that also constructs live API clients and circuit breakers
#      (BinanceClient(), CircuitBreaker(), ...) — there is no side-effect-free
#      way to read them without instantiating the whole fetcher, and
#      database.py is explicitly out of scope for the foundation
#      phase (see module docstring).
#
# This is exactly the kind of drift that is dangerous, so it
# is flagged here rather than pretended away: if any of the five source maps
# below changes, this table goes stale silently. Replacing it with a live,
# tested read of the real maps is a natural wiring-phase task,
# once database.py is back in scope — see the module docstring.
# ---------------------------------------------------------------------------

# database.py :: self.crypto_map keys (221 tickers at snapshot time).
_CRYPTO_SYMBOLS = frozenset({
    'BTC', 'ETH', 'SOL', 'ADA', 'XRP', 'LTC', 'DOGE', 'DOT', 'AVAX', 'LINK',
    'POL', 'UNI', 'ATOM', 'XLM', 'ALGO', 'VET', 'FIL', 'AAVE', 'EOS', 'XTZ',
    'BNB', 'XMR', 'TRX', 'SHIB', 'NEAR', 'SUI', 'APT', 'ARB', 'OP', 'IMX',
    'STRK', 'MANTA', 'ZK', 'METIS', 'LRC', 'SKL', 'FET', 'RENDER', 'TAO',
    'OCEAN', 'AKT', 'AR', 'IO', 'AI', 'CGPT', 'CRV', 'SNX', 'COMP',
    'LDO', 'MKR', 'RUNE', '1INCH', 'GMX', 'PENDLE', 'JUP', 'ENA', 'RPL',
    'COW', 'MORPHO', 'DODO', 'USUAL', 'FRAX', 'GALA', 'SAND', 'MANA', 'AXS',
    'ENJ', 'PRIME', 'BEAM', 'YGG', 'SUPER', 'GMT', 'ALICE', 'RONIN', 'ICP',
    'INJ', 'HBAR', 'FLOW', 'EGLD', 'SEI', 'TIA', 'STX', 'THETA',
    'QNT', 'IOTA', 'KSM', 'ZIL', 'MINA', 'CKB', 'ONE', 'CELO', 'ZEN',
    'ANKR', 'ICX', 'ASTR', 'AXL', 'SC', 'STORJ', 'GLM', 'FLUX', 'RVN',
    'PEPE', 'BONK', 'WIF', 'FLOKI', 'TURBO', 'BRETT', 'BOME', 'PEOPLE',
    'HMSTR', 'LUNC', 'TON', 'ONDO', 'PYTH', 'JTO', 'W', 'RNDR', 'BAND',
    'NMR', 'BAT', 'DASH', 'DCR', 'PAXG', 'NEXO', 'FTT', 'ORCA', 'KMNO',
    'TNSR', 'FIDA', 'BCH', 'ETC', 'NEO', 'ZEC', 'DYDX', 'ENS', 'GRT',
    'SUSHI', 'CAKE', 'LQTY', 'LISTA', 'RDNT', 'RSR', 'APE', 'PIXEL',
    'PORTAL', 'BIGTIME', 'MAGIC', 'ILV', 'AIXBT', 'WLD', 'ARKM', 'JASMY',
    'CHZ', 'ROSE', 'CFX', 'KAVA', 'OSMO', 'LUNA', 'ZRO', 'SAGA', 'DYM',
    'NTRN', 'RAY', 'ACH', 'BLUR', 'SSV', 'ETHFI', 'MOVE', 'BERA', 'KAITO',
    'LAYER', 'HYPER', 'INIT', 'PLUME', 'FORM', 'ORDI', 'NOT', 'DOGS',
    'NEIRO', 'PNUT', 'TRUMP', 'VIRTUAL', 'ANIME', 'EIGEN', 'SHELL',
    'KERNEL', 'SIGN', 'HUMA', 'WAL', 'GRASS', 'COTI', 'BIO', 'PENGU',
    'DEEP', 'CETUS', 'ARC', 'PIPPIN', 'FARTCOIN', 'CHEX', 'GRIFFAIN',
    'COLLAT', 'KIP', 'MLC', 'DSYNC', 'BUTTHOLE', 'S', 'CC', 'HYPE', 'WBT',
    'MON', 'LIT', 'XPL', 'PUMP', 'KPEPE', 'KBONK', 'WLFI', 'VVV', 'ASTER',
    'CHILLGUY', 'AERO', 'PURR',
})

# database.py :: self.crypto_metric_map keys — computed/derived series
# (dominance %, TOTAL* aggregates, cross-asset ratios), never a real
# tradeable instrument on any venue. This is precisely the ALTBTC /
# TOTAL3BTC class this module was built to catch.
_CRYPTO_METRIC_SYMBOLS = frozenset({
    'BTC.D', 'USDT.D', 'TOTAL', 'TOTAL2', 'TOTAL3', 'OTHERS.D',
    'ETHBTC', 'XRPBTC', 'AVAXBTC', 'TOTAL3BTC', 'ALTBTC',
})

# data_sources/binance.py :: BinanceClient.UNAVAILABLE — symbols in
# crypto_map that Binance's spot API cannot serve; they are served by
# Hyperliquid or CCXT instead (see that constant's comment for why).
_BINANCE_UNAVAILABLE = frozenset({
    'XMR', 'CC', 'FARTCOIN', 'AKT', 'ARC', 'BRETT', 'BUTTHOLE',
    'CHEX', 'COLLAT', 'DEEP', 'DSYNC', 'GRASS', 'GRIFFAIN', 'KIP', 'MLC',
    'PIPPIN', 'PRIME', 'RNDR', 'OCEAN', 'BEAM', 'MKR', 'NTRN',
    'RDNT', 'TON', 'EOS',
})

# data_sources/hyperliquid.py :: HyperliquidClient.HL_COINS — HL-first
# listings (mostly recovered from BINANCE_UNAVAILABLE). Checked BEFORE
# _BINANCE_UNAVAILABLE below because several tickers appear in both sets
# (FARTCOIN, GRIFFAIN, GRASS, BRETT) and HL is where crypto_map's own
# comment says they are actually served from.
_HYPERLIQUID_COINS = frozenset({
    'HYPE', 'FARTCOIN', 'MON', 'LIT', 'KPEPE', 'VVV', 'GRIFFAIN', 'KBONK',
    'CHILLGUY', 'AERO', 'GRASS', 'BRETT', 'PURR',
})

# data_sources/stooq.py :: stock_map | etf_map | index_map keys, in full —
# routed to Stooq as primary source (Yahoo fallback, not tracked as
# authoritative here). stock_map (51) includes RHM and SQ, index_map (17)
# includes BUX and HSTECH — all four of the measured reverse
# orphans, which is a useful cross-check that this snapshot is accurate.
_STOOQ_STOCK_ETF_INDEX = frozenset({
    # stock_map (51)
    'AAPL', 'GOOG', 'AMZN', 'META', 'TSLA', 'NVDA', 'MSFT', 'AMD',
    'INTC', 'QCOM', 'BA', 'LMT', 'RTX', 'NOC', 'GD', 'RHM',
    'HON', 'XOM', 'CVX', 'JPM', 'GS', 'V', 'MA', 'UNH',
    'JNJ', 'PG', 'KO', 'WMT', 'FMNB', 'AM', 'BYDDY',
    'MSTR', 'COIN', 'RIOT', 'MARA', 'HOOD', 'SOFI', 'SQ', 'PYPL',
    'PLTR', 'ARM', 'SMCI', 'CRWD', 'NET', 'SNOW', 'NFLX', 'DIS',
    'UBER', 'LLY', 'PFE', 'TNK',
    # etf_map (29)
    'VTI', 'QQQ', 'IWM', 'XLE', 'XLF', 'XLK', 'XLV', 'EEM',
    'EWJ', 'EWZ', 'FXI', 'KWEB', 'GDX', 'URA', 'MAGS', 'TLT',
    'HYG', 'LQD', 'SHY', 'ARKK', 'IBIT', 'BITO', 'SOXX', 'SMH',
    'IGV', 'XBI', 'VNQ', 'XLP', 'XLI',
    # index_map (17)
    'SPX', 'SPY', 'NDX', 'DJI', 'RUT', 'VIX', 'DAX', 'FTSE',
    'CAC', 'BUX', 'NIKKEI', 'HSI', 'HSTECH', 'KOSPI', 'US10Y', 'US30Y',
    'US5Y',
})

# data_sources/stooq.py :: WRONG_SCALE_ON_STOOQ — commodity_map | forex_map
# in full (both are wholly contained in WRONG_SCALE_ON_STOOQ:
# 9 commodities + 10 FX pairs = all 19 entries). Stooq SUCCEEDS
# for these but returns the wrong INSTRUMENT (an ETF proxy on a different
# scale), so Yahoo — not Stooq — is authoritative.
_STOOQ_WRONG_SCALE = frozenset({
    'OIL', 'GOLD', 'SILVER', 'COPPER', 'NATGAS', 'PLATINUM', 'CORN',
    'WHEAT', 'SOYBEANS',
    'DXY', 'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF',
    'NZDUSD', 'EURHUF', 'USDHUF',
})


# Reverse index: on-disk stem -> the dotted ticker the routing maps know.
#
# Needed because ``reconcile`` learns a ticker's dotted form from the MANIFEST,
# and an orphan has no manifest entry — so it would fall back to the mangled
# stem, and ``derive_authoritative_source('BTC_D')`` is 'unknown' where
# ``('BTC.D')`` is 'derived'. If BTC.D ever dropped out of the manifest while
# keeping its cache file, its source would silently flip to 'unknown' and Check E
# (frozen_derived_series, scripts/cache_integrity_check.py) would stop
# covering it — the dot-mangling bug in a second disguise.
#
# The maps themselves are the authority on which dotted tickers exist, so this
# asks them rather than trying to invert the mangling (which is not invertible in
# general: a real ticker may legitimately contain an underscore).
_STEM_TO_KNOWN_TICKER = {}
for _grp in (_CRYPTO_METRIC_SYMBOLS, _CRYPTO_SYMBOLS, _BINANCE_UNAVAILABLE,
             _HYPERLIQUID_COINS, _STOOQ_STOCK_ETF_INDEX, _STOOQ_WRONG_SCALE):
    for _t in _grp:
        _stem = _cache_stem(_t)
        # Only record stems that actually differ from the ticker; an identity
        # mapping buys nothing and would mask a genuine collision.
        if _stem != _t.upper():
            _STEM_TO_KNOWN_TICKER.setdefault(_stem, _t)
del _grp, _t, _stem


def known_ticker_for_stem(stem: str) -> Optional[str]:
    """The dotted ticker whose cache file is ``stem``, if the routing maps know
    one. ``None`` otherwise — callers keep their own fallback."""
    return _STEM_TO_KNOWN_TICKER.get((stem or '').upper())


def derive_authoritative_source(ticker: str) -> str:
    """Which feed owns ``ticker``, derived from the routing-map snapshot
    above. Returns 'unknown' for anything not covered — the
    instruction is explicit: populate what is mechanically derivable, and
    default to 'unknown' otherwise, never guess.

    Values used: 'binance', 'hyperliquid', 'cryptocompare' (legacy label
    persisted in instrument records for symbols Binance's spot API cannot
    serve; they are now fetched via CCXT), 'stooq', 'yahoo', 'derived' (a computed/aggregate series, not a real instrument
    on any venue — the ALTBTC/TOTAL3BTC class), 'unknown'.
    """
    t = (ticker or '').upper()
    if t in _CRYPTO_METRIC_SYMBOLS:
        return 'derived'
    if t in _CRYPTO_SYMBOLS:
        if t in _HYPERLIQUID_COINS:
            return 'hyperliquid'
        if t in _BINANCE_UNAVAILABLE:
            return 'cryptocompare'
        return 'binance'
    if t in _STOOQ_WRONG_SCALE:
        return 'yahoo'
    if t in _STOOQ_STOCK_ETF_INDEX:
        return 'stooq'
    return 'unknown'


def is_derived_series(ticker) -> bool:
    """True when ``ticker`` is a computed/aggregate series rather than a real
    instrument traded on some venue — the ALTBTC / TOTAL3BTC class.

    This is the one question four guards otherwise answer by
    LOOKING AT THE BARS: a series in which every row is flat OHLC with zero
    volume was assumed to be using that shape as its normal encoding, because
    the derived ratio feeds genuinely do. The assumption is unfalsifiable from
    the bars alone.

    Accepts either form of the name. ``csv_cache.get_path`` mangles dots, so the
    guards routinely hold ``BTC_D`` where the routing maps know ``BTC.D`` — and
    ``derive_authoritative_source('BTC_D')`` is 'unknown'. Answering from the
    mangled stem without recovering the dotted ticker would silently strip BTC.D
    of its exemption; that is the dot-mangling bug in a third disguise.

    Deliberately reads the routing-map snapshot rather than the
    ``_instrument_master.json`` store, even though the store carries the same
    field. ``reconcile()`` fills that field by calling this module's
    ``derive_authoritative_source``, so the store is a cached copy of this
    answer — and a copy that can be missing, empty or stale. The write guard
    consults this on every daily save; a pure frozenset lookup has no failure
    mode to degrade from, which is why nothing here mirrors the
    store-unavailable branch of the integrity checks.
    """
    t = (ticker or '').upper()
    if derive_authoritative_source(t) == 'derived':
        return True
    known = known_ticker_for_stem(t)
    return known is not None and derive_authoritative_source(known) == 'derived'


# ---------------------------------------------------------------------------
# Reconciliation — reconcile FILES
# against MANIFEST, not manifest-only iteration.
# ---------------------------------------------------------------------------

def reconcile(cache_dir=None, data_dir: Optional[str] = None,
              now: Optional[datetime] = None,
              existing: Optional[Dict[str, InstrumentRecord]] = None
              ) -> Dict[str, InstrumentRecord]:
    """Scan ``cache_dir`` + the tracked-symbols manifest and produce/update
    the instrument-master record set.

    Every ``*_daily.csv`` file gets a record (the 28 orphans), and
    every manifest entry with no file gets one too (the 4 reverse orphans).
    The join key between a manifest ticker and its cache file is the
    dot-mangled ``_cache_stem``, which avoids the false-orphan case
    (BTC.D vs BTC_D) described above.

    ``existing`` — the already-loaded record set (see ``load_records``)
    — makes this call apply knowledge-date semantics: a record whose status
    is unchanged from ``existing`` gets ``last_confirmed`` advanced but keeps
    its original ``first_observed``; a record with no prior entry gets both
    set to ``now``. Reconciliation itself never changes ``status`` — that is
    ``apply_status_updates``'s job — so in practice every call here just
    advances ``last_confirmed`` for already-seen ids. Passing no
    ``existing`` (the default) starts every id fresh.

    Never deletes: a symbol that vanishes from the manifest AND loses its
    cache file simply stops appearing in the OUTPUT of this call (there is
    nothing left to reconcile), but nothing here deletes bars, files, or a
    caller's own copy of a previous record set — that decision belongs to
    whatever calls ``write_store`` with the result, in the wiring phase.
    """
    resolved_data_dir = data_dir or os.environ.get('DATA_DIR', 'data')
    resolved_cache_dir = cache_dir or resolved_data_dir
    now_iso = _now_iso(now)
    existing = existing or {}

    files_by_stem = list_cache_files(resolved_cache_dir)
    manifest_symbols = read_tracked_manifest(resolved_data_dir)

    stem_to_ticker: Dict[str, str] = {}
    for ticker in manifest_symbols:
        stem_to_ticker[_cache_stem(ticker)] = ticker

    all_stems = set(files_by_stem) | set(stem_to_ticker)

    records: Dict[str, InstrumentRecord] = {}
    for stem in sorted(all_stems):
        has_file = stem in files_by_stem
        in_manifest = stem in stem_to_ticker
        # Manifest first (it is the live truth about dotted forms), then the
        # routing maps' reverse index, and only then the mangled stem itself.
        # Without the middle step an orphaned BTC.D file classifies 'unknown'
        # instead of 'derived' — see known_ticker_for_stem for the full why.
        ticker = stem_to_ticker.get(stem) or known_ticker_for_stem(stem) or stem

        if has_file and in_manifest:
            tracking_state = 'tracked'
        elif has_file:
            tracking_state = 'orphaned'
        else:
            tracking_state = 'reverse_orphan'

        if has_file:
            first_bar, last_bar, row_count = _scan_cache_file(files_by_stem[stem])
        else:
            first_bar, last_bar, row_count = (None, None, None)

        prior = existing.get(stem)
        status = prior.status if prior else DEFAULT_STATUS
        authoritative_source = derive_authoritative_source(ticker)

        if prior is not None:
            first_observed = prior.first_observed or now_iso
            last_confirmed = now_iso
        else:
            first_observed = now_iso
            last_confirmed = now_iso

        records[stem] = InstrumentRecord(
            id=stem,
            ticker=ticker,
            cache_stem=stem,
            status=status,
            tracking_state=tracking_state,
            venue=authoritative_source,
            authoritative_source=authoritative_source,
            first_bar_date=first_bar,
            last_bar_date=last_bar,
            row_count=row_count,
            first_observed=first_observed,
            last_confirmed=last_confirmed,
            delist_candidate_since=(prior.delist_candidate_since if prior else None),
        )
    # The curated list is the final word on status, so it is
    # stamped here too — a caller that only reconciles (no venue fetch) still
    # gets the delisted verdicts. Idempotent, so the collector re-applying it
    # after the Binance ingestion is a correction of ORDER, not a double write.
    return apply_lifecycle_overrides(records, now=now)


# ---------------------------------------------------------------------------
# Binance exchangeInfo status ingestion
#
# Called from carrac/precompute.py::_write_instrument_master (see the module
# docstring). Also callable standalone:
# ``binance_status_by_ticker(fetch_binance_exchange_info(), ...)``
# then ``apply_status_updates(records, ...)``. The network call is injectable
# via the ``fetch`` parameter specifically so tests never touch the real
# Binance API and never monkeypatch this module's own functions.
# ---------------------------------------------------------------------------

_BINANCE_EXCHANGE_INFO_URL = 'https://data-api.binance.vision/api/v3/exchangeInfo'

# Binance spot exchangeInfo status values -> status vocabulary. TRADING
# and BREAK are Binance's own two live states; HALT is included
# defensively (seen on some Binance endpoints, undocumented for spot) and
# treated the same as BREAK — both mean "not currently tradeable, but not
# necessarily gone". Anything else, including simple absence from the
# response, maps to 'unknown' rather than 'delisted': Binance's spot
# exchangeInfo can omit a symbol for reasons other than delisting (a
# transient fetch gap, a symbol that was never spot-listed at all — e.g.
# every Hyperliquid-first ticker), and asserting 'delisted' from absence
# would be exactly the kind of guess the status vocabulary rules out.
_BINANCE_STATUS_MAP = {
    'TRADING': 'trading',
    'BREAK': 'suspended',
    'HALT': 'suspended',
}


def map_binance_status(raw_status: Optional[str]) -> str:
    """Binance's raw per-symbol status string -> status vocabulary."""
    if not raw_status:
        return 'unknown'
    return _BINANCE_STATUS_MAP.get(str(raw_status).upper(), 'unknown')


# Minimum plausible size for a Binance spot exchangeInfo payload.
#
# PRECONDITION for lifecycle decisions. The only health check on this payload today is
# `info.get('symbols')` truthiness, and that is not enough for a payload the
# system is about to make LIFECYCLE decisions from. Two failures pass it:
#
#   * a short/partial response — every ticker absent from it maps to 'unknown'
#     (never 'delisted', which is the load-bearing asymmetry in
#     `binance_status_by_ticker`), so one degraded fetch silently ERASES the
#     accumulated venue evidence. Measured on real data, that is 13
#     `binance_status=suspended` records going back to `unknown` — the exact
#     evidence that lifecycle-based collection gating would rely on.
#   * a well-formed response for a different endpoint or a stub.
#
# The rule: treat the payload as usable ONLY when it
# is demonstrably healthy, never when it is empty or short. A real response
# carried 3681 symbols when measured, so a floor of 1000 sits far below any
# healthy response and far above any degraded one — the same "two populations
# with an empty band between them" construction as csv_cache's
# WEEKEND_TRADER_RATIO, and equally not load-bearing on its exact value.
EXCHANGE_INFO_MIN_SYMBOLS = 1000


def is_healthy_exchange_info(payload: Optional[dict]) -> bool:
    """Is this exchangeInfo payload safe to draw conclusions from?

    Returns False for empty, malformed and SHORT payloads alike. Callers must
    treat False as "no observation was made" and change nothing — not as
    "every symbol is unknown", which is how a transient blip would otherwise
    wipe the venue evidence.
    """
    symbols = (payload or {}).get('symbols')
    if not isinstance(symbols, list):
        return False
    return len(symbols) >= EXCHANGE_INFO_MIN_SYMBOLS


def venue_status_coverage(records: Dict[str, 'InstrumentRecord']) -> dict:
    """How much venue evidence the store actually holds right now.

    ``known`` is the number that matters: records where Binance has positively
    told us something. A drop in it means we are losing sight of the venue,
    which is invisible in the summary log line and is precisely what lifecycle-based
    collection gating must not be allowed to gate on silently.
    """
    counts = {'trading': 0, 'suspended': 0, 'unknown': 0}
    for rec in records.values():
        counts[rec.binance_status] = counts.get(rec.binance_status, 0) + 1
    counts['known'] = counts.get('trading', 0) + counts.get('suspended', 0)
    counts['total'] = len(records)
    return counts


def _default_binance_fetch() -> dict:
    import urllib.request
    with urllib.request.urlopen(_BINANCE_EXCHANGE_INFO_URL, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))


def fetch_binance_exchange_info(fetch=None) -> dict:
    """Fetch Binance's spot exchangeInfo payload.

    ``fetch`` is injectable — pass a zero-arg callable returning the parsed
    JSON dict to avoid a real network call (tests always do this). Defaults
    to a stdlib ``urllib.request`` call so this stays importable without
    pandas or ``requests``, consistent with the rest of this module.

    Never raises: any failure (network, timeout, bad JSON) yields ``{}``,
    which every downstream consumer here treats as "no data", mapping every
    requested ticker to 'unknown' rather than crashing a caller that expects
    a best-effort status refresh, not a hard dependency.
    """
    fetch = fetch or _default_binance_fetch
    try:
        payload = fetch()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def binance_status_by_ticker(exchange_info: dict, tickers: Iterable[str],
                              pair_suffix: str = 'USDT') -> Dict[str, str]:
    """Map exchangeInfo's per-PAIR status onto Carrac's per-TICKER vocabulary
    for exactly the ``tickers`` requested — callers should pass symbols
    already known to be Binance-routed (``derive_authoritative_source(t) ==
    'binance'``), not the whole exchange universe.
    """
    status_by_pair = {}
    for entry in (exchange_info or {}).get('symbols', []) or []:
        pair = entry.get('symbol')
        if pair:
            status_by_pair[pair] = entry.get('status')

    out = {}
    for ticker in tickers:
        t = ticker.upper()
        out[t] = map_binance_status(status_by_pair.get(f"{t}{pair_suffix}"))
    return out


def apply_status_updates(records: Dict[str, InstrumentRecord],
                          status_by_ticker: Dict[str, str],
                          now: Optional[datetime] = None
                          ) -> Dict[str, InstrumentRecord]:
    """Apply new status observations onto an existing record set.

    Knowledge-date semantics: if the observed status
    matches the record's current status, only ``last_confirmed`` advances —
    ``first_observed`` is untouched. If it differs, BOTH reset to ``now``,
    because a new status value means a new fact we are now dating from
    scratch. Membership (which ids exist) is owned by ``reconcile`` — this
    function only updates records that are already present; unmatched
    tickers in ``status_by_ticker`` are silently ignored, not added.
    """
    now_iso = _now_iso(now)
    updated = dict(records)
    for stem, record in records.items():
        new_status = status_by_ticker.get(record.ticker.upper())
        if new_status is None or new_status == record.status:
            if new_status == record.status:
                updated[stem] = replace(
                    record,
                    last_confirmed=now_iso,
                    first_observed=record.first_observed or now_iso,
                )
            continue
        updated[stem] = replace(
            record,
            status=new_status,
            first_observed=now_iso,
            last_confirmed=now_iso,
        )
    return updated


# Feeds whose symbols are, or once were, Binance-listed pairs. Binance's
# exchangeInfo can speak about all of them, so all of them get asked.
#
# Prerequisite for lifecycle decisions. The scope is circular if narrowed: a
# ticker is routed off Binance (_BINANCE_UNAVAILABLE) *because* Binance
# stopped serving it, so asking Binance only about tickers still routed to
# Binance leaves the symbols whose listing status matters most never asked
# about. Measured on real data under that narrower scope: 194 of 380 records sat
# at status 'unknown', and every stale symbol was in that bucket.
_VENUE_STATUS_SOURCES = frozenset({'binance', 'cryptocompare', 'hyperliquid'})


def venue_status_tickers(records: Dict[str, 'InstrumentRecord']) -> list:
    """Tickers worth asking Binance's exchangeInfo about.

    Every crypto-routed ticker, not just the Binance-routed ones — see
    ``_VENUE_STATUS_SOURCES``. Stooq/Yahoo/derived symbols are excluded:
    Binance has nothing to say about AAPL or ALTBTC, and asking would only
    manufacture 'unknown' answers that look like observations.
    """
    return [
        rec.ticker for rec in records.values()
        if derive_authoritative_source(rec.ticker) in _VENUE_STATUS_SOURCES
    ]


def apply_binance_venue_status(records: Dict[str, 'InstrumentRecord'],
                               status_by_ticker: Dict[str, str],
                               now: Optional[datetime] = None
                               ) -> Dict[str, 'InstrumentRecord']:
    """Record what Binance says about each pair, WITHOUT touching ``status``.

    Mirrors ``apply_status_updates``'s knowledge-date semantics on its own
    timestamp: ``binance_status_at`` advances on every confirmation, so a
    reader can always tell how fresh the venue's verdict is rather than
    trusting the field's existence.

    The absence asymmetry from ``binance_status_by_ticker`` is preserved and
    load-bearing: a ticker Binance does not list maps to 'unknown', never to
    'delisted'. One failed fetch must not be able to condemn the universe.
    """
    now_iso = _now_iso(now)
    updated = dict(records)
    for stem, record in records.items():
        observed = status_by_ticker.get(record.ticker.upper())
        if observed is None:
            continue
        updated[stem] = replace(
            record,
            binance_status=observed,
            binance_status_at=now_iso,
        )
    return updated


# ---------------------------------------------------------------------------
# Lifecycle overrides — the ONLY thing that sets 'delisted'
#
# The decision: a positively-asserted,
# version-controlled list is the sole writer of ``status='delisted'``. The
# conjunction rule below (venue says suspended AND the bars stopped) only
# ever PROPOSES candidates to a human; it never writes a status.
#
# Why not let the rule set it directly. The rule's inputs are two weak
# signals and it would be a one-way ratchet: the delisted status stops collecting a
# delisted symbol, so a wrongly-condemned symbol can never produce the
# fresh bar that would clear it. Measured on real data, a 14-day
# threshold would condemn OCEAN, NTRN, RNDR and FTM together — four
# instruments whose bars all stopped within the same five-day span, which is one
# upstream-feed event, not four delistings. That is precisely the
# "inference dressed up as a fact" this module exists to remove.
#
# This also honours the contract stated at the top of this module —
# "'delisted' is populated by something that can positively assert it (not
# by absence from a single feed's snapshot)" — and generalises the existing
# ``database.RETIRED_SYMBOLS`` stopgap, whose own comment names this lifecycle
# mechanism as the one meant to replace it.
#
# A module constant rather than a JSON file in ``data/``, deliberately:
#  * the write guard (``csv_cache.CSVCache.save``) consults this on every
#    daily save, and a frozenset/dict lookup has no failure mode to degrade
#    from — the same reasoning that keeps ``is_derived_series`` reading the
#    routing-map snapshot instead of the store;
#  * condemning an instrument is a reviewable act. In git it has an author,
#    a date and a diff; in a data file written by a cron it has none.
#
# RETENTION IS NEVER AFFECTED (the survivorship rule). Nothing here
# deletes a bar, a row or a file. ``delisted`` gates COLLECTION and
# PUBLISHING only — backtests, analogues and historical charts keep every
# bar Carrac ever stored, because excluding dead instruments from a backtest
# overstates returns by 1-4% annually.
#
# ``successor`` is recorded where the instrument continued under another
# ticker. The record schema has no rename-history field yet (the design says leave room for one); keeping it here means the
# knowledge is not lost in the meantime, and it is the only field in this
# table sourced from outside Carrac's own observations — hence stated as
# belief, with the observed evidence in ``note`` kept separate from it.
# ---------------------------------------------------------------------------

# Reason vocabulary. 'migrated' = the instrument continued under a new
# ticker; 'venue_delisted' = the venue stopped the pair and Carrac has no
# other source for it. Both end in status 'delisted' — they differ in what
# a human should do next, which is why they are not collapsed.
LIFECYCLE_REASONS = ('migrated', 'venue_delisted', 'owner_retired')

LIFECYCLE_OVERRIDES = {
    # Seeded from the 13 symbols Binance reported BREAK for, split
    # by whether Carrac still receives bars. These nine had stopped; BEAM,
    # LIT, RDNT and XMR were all current and are deliberately absent.
    'AGIX': {
        'status': 'delisted', 'reason': 'migrated', 'successor': 'FET',
        'effective_date': '2024-07-08',
        'note': 'Last stored bar 2024-07-08; 761 fabricated flat bars followed '
                'it. Binance reports BREAK.',
    },
    'OCEAN': {
        'status': 'delisted', 'reason': 'migrated', 'successor': 'FET',
        'effective_date': '2026-08-08',
        'note': 'Last stored bar 2026-08-08. Binance reports BREAK.',
    },
    'FTM': {
        'status': 'delisted', 'reason': 'migrated', 'successor': 'S',
        'effective_date': '2026-08-04',
        'note': 'Last stored bar 2026-08-04. Binance reports BREAK. FTM is also '
                'the symbol that was served with a live price and a '
                '362% 24h change against zero OHLCV rows.',
    },
    'MKR': {
        'status': 'delisted', 'reason': 'migrated', 'successor': 'SKY',
        'effective_date': '2026-07-30',
        'note': 'Last stored bar 2026-07-30. Binance reports BREAK.',
    },
    'RNDR': {
        'status': 'delisted', 'reason': 'migrated', 'successor': 'RENDER',
        'effective_date': '2026-08-07',
        'note': 'Last stored bar 2026-08-07. Binance reports BREAK.',
    },
    'EOS': {
        'status': 'delisted', 'reason': 'migrated', 'successor': 'A',
        'effective_date': '2026-07-03',
        'note': 'Last stored bar 2026-07-03. Binance reports BREAK.',
    },
    'LRC': {
        'status': 'delisted', 'reason': 'venue_delisted', 'successor': None,
        'effective_date': '2026-06-09',
        'note': 'Last stored bar 2026-06-09. Binance reports BREAK and Binance '
                'is its authoritative source, so this is the one symbol here '
                'whose instrument-level status the venue could set on its own.',
    },
    'NTRN': {
        'status': 'delisted', 'reason': 'venue_delisted', 'successor': None,
        'effective_date': '2026-08-07',
        'note': 'Last stored bar 2026-08-07. Binance reports BREAK.',
    },
    'COLLAT': {
        'status': 'delisted', 'reason': 'venue_delisted', 'successor': None,
        'effective_date': '2026-07-27',
        'note': 'Last stored bar 2026-07-27; file not written since '
                '2026-08-11. On no venue Carrac reads (checked Binance, '
                'Hyperliquid and all four CCXT exchanges during the '
                'removal of the old fallback vendor, which returned 0 rows '
                'for it even before it was removed). Recorded as '
                'a deliberate decision.',
    },
    'TON': {
        'status': 'delisted', 'reason': 'venue_delisted', 'successor': None,
        'effective_date': '2026-06-30',
        'note': 'Last stored bar 2026-06-30. Binance reports BREAK. Recorded '
                'deliberately: Toncoin trades actively on venues '
                'Carrac does not read, so this says "Carrac has no source for '
                'it", not "the instrument is dead". Publishing a 54-day-old '
                'price as current is the worse of the two errors.',
    },
    # Retired deliberately. All four are still tradable on gate.io — 'owner_
    # retired' says "Carrac chose to stop", not "the venue stopped". Each had
    # already stopped collecting when it fell off the CoinPaprika top-coins
    # list (the OHLCV funnel's crypto verdict never consults the CCXT routing
    # table), and since the crypto-provenance fix the funnel refuses them outright, so their
    # cache files are unreachable dead weight, kept as-is.
    'SIREN': {
        'status': 'delisted', 'reason': 'owner_retired', 'successor': None,
        'effective_date': '2026-08-25',
        'note': 'Microcap; last stored bar 2026-06-09 (froze when it left the '
                'top-coins list). Tradable on gate.io — relist by adding to '
                'crypto_map if ever wanted.',
    },
    'BDX': {
        'status': 'delisted', 'reason': 'owner_retired', 'successor': None,
        'effective_date': '2026-08-25',
        'note': 'The crypto Beldex. Its file is genuine gate.io history '
                'truncated at 2025-04-10 by a short-page bug '
                '(the pre-08-23 file was Becton Dickinson, quarantined by '
                'the crypto-provenance fix). ⚠ Name collides with the BDX equity — if '
                'Becton Dickinson is ever curated into stock_map, this '
                'entry must be revisited first.',
    },
    'FDUSD': {
        'status': 'delisted', 'reason': 'owner_retired', 'successor': None,
        'effective_date': '2026-08-25',
        'note': 'Stablecoin; last stored bar 2026-07-17 (froze when it left '
                'the top-coins list). Tradable on gate.io and Binance — '
                'relist by adding to crypto_map if ever wanted.',
    },
    'DEXE': {
        'status': 'delisted', 'reason': 'owner_retired', 'successor': None,
        'effective_date': '2026-08-25',
        'note': 'Last stored bar 2026-07-17, same top-coins-list cutoff as '
                'FDUSD. Tradable on gate.io and Binance — relist by adding '
                'to crypto_map if ever wanted.',
    },
    # Stale-symbol cleanup. The seven series the
    # daily cache-integrity guard had reported STALE every morning (12..127
    # days). Venue status checked by hand that day. The four still tradable
    # somewhere are owner_retired, not venue_delisted: Carrac's route died,
    # the coin did not — relisting is a re-route in crypto_map, not a revert.
    'KAS': {
        'status': 'delisted', 'reason': 'owner_retired', 'successor': None,
        'effective_date': '2026-09-29',
        'note': 'Last stored bar 2026-08-08. Routed to Binance, which no '
                'longer lists KASUSDT ("Invalid symbol"). Tradable on Kraken '
                '(KAS/USD) and gate.io — relist by re-routing, not reverting.',
    },
    'STORJ': {
        'status': 'delisted', 'reason': 'owner_retired', 'successor': None,
        'effective_date': '2026-09-29',
        'note': 'Last stored bar 2026-09-03. Binance STORJUSDT is BREAK and '
                'Coinbase STORJ-USD is delisted; still tradable on gate.io.',
    },
    'ICX': {
        'status': 'delisted', 'reason': 'owner_retired', 'successor': None,
        'effective_date': '2026-09-29',
        'note': 'Last stored bar 2026-09-03. Binance ICXUSDT is BREAK; still '
                'tradable on KuCoin (ICX-USDT).',
    },
    'CHILLGUY': {
        'status': 'delisted', 'reason': 'owner_retired', 'successor': None,
        'effective_date': '2026-09-29',
        'note': 'Last real bar 2026-05-25. Hyperliquid-routed; the fetch kept '
                'rewriting the file daily with nothing new. Listed on Bybit '
                '(CHILLGUYUSDT).',
    },
    'BUTTHOLE': {
        'status': 'delisted', 'reason': 'venue_delisted', 'successor': None,
        'effective_date': '2026-09-29',
        'note': 'Last stored bar 2026-08-07. MEXC (its route) returns no '
                'market; no spot market on any of the 9 venues checked for '
                'the TradingView map on 2026-09-23.',
    },
    'KIP': {
        'status': 'delisted', 'reason': 'venue_delisted', 'successor': None,
        'effective_date': '2026-09-29',
        'note': 'Last stored bar 2026-09-16. gate.io (its route) reports '
                '"currency KIP is delisted"; no venue in the TradingView map.',
    },
    'GRAM': {
        'status': 'delisted', 'reason': 'owner_retired', 'successor': None,
        'effective_date': '2026-08-23',
        'note': 'GRAM.US / Gold Flora, a defunct OTC stock — NOT the crypto. '
                'Already in database.RETIRED_SYMBOLS (the stopgap this table '
                'generalises) since 2026-08-23, but still reported STALE by '
                'Check F because nothing here said so. Last real bar '
                '2026-07-03.',
    },
}

# Keyed by cache stem so a caller holding either form of the name resolves:
# the guards routinely hold the dot-mangled 'BTC_D' where the routing maps
# know 'BTC.D' (see _cache_stem). Nothing in the seed set has a dot today;
# keying this way means nothing breaks the first time one does.
_LIFECYCLE_OVERRIDES_BY_STEM = {
    _cache_stem(ticker): dict(entry, ticker=ticker)
    for ticker, entry in LIFECYCLE_OVERRIDES.items()
}


def lifecycle_override_for(ticker) -> Optional[dict]:
    """The curated lifecycle entry for ``ticker``, or None.

    Accepts the dotted ticker or the mangled cache stem — both canonicalise
    to the same key.
    """
    if not ticker:
        return None
    return _LIFECYCLE_OVERRIDES_BY_STEM.get(_cache_stem(str(ticker)))


def is_delisted(ticker) -> bool:
    """True when ``ticker`` is on the curated delisted list.

    The predicate the collection filter and the ingest guard both call. Reads
    the module constant, never the store: no file I/O, no missing/empty/stale
    failure modes, and safe to call per-save. Mirrors ``is_derived_series``.
    """
    entry = lifecycle_override_for(ticker)
    return bool(entry) and entry.get('status') == 'delisted'


def delisted_tickers() -> set:
    """Every ticker the curated list marks delisted, in its dotted form."""
    return {
        entry['ticker'] for entry in _LIFECYCLE_OVERRIDES_BY_STEM.values()
        if entry.get('status') == 'delisted'
    }


def apply_lifecycle_overrides(records: Dict[str, InstrumentRecord],
                              now: Optional[datetime] = None
                              ) -> Dict[str, InstrumentRecord]:
    """Stamp the curated statuses onto a record set — the FINAL word.

    Called last, after ``reconcile`` and after any venue ingestion, because a
    curated verdict outranks a derived one by construction: LRC is 'suspended'
    to Binance and 'delisted' to us, and the order of these two calls is the
    only thing that decides which survives.

    Knowledge dates follow ``apply_status_updates``: unchanged status advances
    ``last_confirmed`` only; a changed status resets both, because a new value
    is a new fact dated from scratch. A ticker with no record is ignored rather
    than invented — membership is ``reconcile``'s to own.
    """
    now_iso = _now_iso(now)
    updated = dict(records)
    for stem, record in records.items():
        entry = _LIFECYCLE_OVERRIDES_BY_STEM.get(stem)
        if not entry:
            continue
        new_status = entry.get('status')
        if new_status not in STATUSES:
            continue
        if new_status == record.status:
            updated[stem] = replace(
                record,
                last_confirmed=now_iso,
                first_observed=record.first_observed or now_iso,
                delist_candidate_since=None,
            )
            continue
        updated[stem] = replace(
            record,
            status=new_status,
            first_observed=now_iso,
            last_confirmed=now_iso,
            delist_candidate_since=None,
        )
    return updated


# ---------------------------------------------------------------------------
# The proposer — venue-suspended AND stale. Never writes a status.
# ---------------------------------------------------------------------------

# 30 days, chosen from the measured distribution rather than from taste. The
# staleness of the 13 Binance-BREAK symbols when measured, in days:
#
#     0 0 0 0   BEAM LIT RDNT XMR    (current, must never be touched)
#     15 16 16 19 24                 (one upstream-feed event, same five days)
#     ── 30 ──
#     51 54 75 766                   EOS TON LRC AGIX
#
# The two widest empty bands are 0->15 and 24->51. This sits in the second,
# so a source-side outage of a fortnight cannot produce a proposal, and the
# threshold is not load-bearing on its exact value — the same
# two-populations-with-an-empty-band construction as EXCHANGE_INFO_MIN_SYMBOLS
# and csv_cache's WEEKEND_TRADER_RATIO.
DELIST_CANDIDATE_STALE_DAYS = 30


def _bar_age_days(last_bar_date: Optional[str], now: Optional[datetime] = None
                  ) -> Optional[int]:
    """Age in days of an ISO ``YYYY-MM-DD`` bar date, or None if unusable."""
    if not last_bar_date:
        return None
    try:
        bar = date.fromisoformat(str(last_bar_date)[:10])
    except (TypeError, ValueError):
        return None
    return ((now or datetime.now()).date() - bar).days


def delist_candidates(records: Dict[str, InstrumentRecord],
                      now: Optional[datetime] = None,
                      min_stale_days: int = DELIST_CANDIDATE_STALE_DAYS
                      ) -> list:
    """Symbols the conjunction rule proposes for the curated list.

    The conjunction is the whole safety property: the venue must POSITIVELY
    say 'suspended' (never 'unknown' — absence is not evidence, and one failed
    exchangeInfo fetch maps every Binance-routed ticker to 'unknown') AND the
    stored bars must have stopped for longer than ``min_stale_days``. Either
    half alone condemns live assets: BEAM, LIT, RDNT and XMR are all BREAK on
    Binance while trading elsewhere with current bars, and plenty of healthy
    symbols go briefly quiet.

    Returns dicts, not tickers, so the alarm can say WHY without re-deriving
    it. Already-curated symbols are excluded — a proposal about a decision
    already taken is noise, and noise is how a guard trains you to ignore it.
    """
    out = []
    for record in records.values():
        if record.binance_status != 'suspended':
            continue
        if lifecycle_override_for(record.ticker):
            continue
        age = _bar_age_days(record.last_bar_date, now)
        if age is None or age < min_stale_days:
            continue
        out.append({
            'ticker': record.ticker,
            'last_bar_date': record.last_bar_date,
            'age_days': age,
            'authoritative_source': record.authoritative_source,
            'already_flagged': record.delist_candidate_since is not None,
        })
    return sorted(out, key=lambda c: (-c['age_days'], c['ticker']))


def mark_delist_candidates(records: Dict[str, InstrumentRecord],
                           candidates: list,
                           now: Optional[datetime] = None):
    """Record candidacy on the store and report only what is NEW.

    Returns ``(records, newly_flagged)``. ``delist_candidate_since`` is set on
    a symbol the first time it qualifies and cleared as soon as it stops, so
    the caller's alarm fires on the transition rather than on every cycle —
    an alarm that repeats a standing condition every 30 minutes is how a guard becomes
    noise.
    """
    now_iso = _now_iso(now)
    by_stem = {_cache_stem(c['ticker']): c for c in candidates}
    updated = dict(records)
    newly_flagged = []
    for stem, record in records.items():
        candidate = by_stem.get(stem)
        if candidate is None:
            if record.delist_candidate_since is not None:
                updated[stem] = replace(record, delist_candidate_since=None)
            continue
        if record.delist_candidate_since is None:
            updated[stem] = replace(record, delist_candidate_since=now_iso)
            newly_flagged.append(candidate)
    return updated, newly_flagged


# ---------------------------------------------------------------------------
# TradingView symbol map (venue-exact feed split)
# ---------------------------------------------------------------------------
#
# THE one map between a Carrac ticker and a TradingView symbol. There is no
# second one: ``to_tv_symbol`` / ``from_tv_symbol`` are the only mapping
# functions, and ``tv_symbol_map()`` is the only table.
#
# Venue-exact. A TradingView exchange prefix names a venue. An exchange-listed
# crypto chart symbol (COINBASE:LTCUSD) resolves to THAT venue's market as ccxt
# reads it — venue pair name, tick size — or raises ``UnsupportedVenueError``.
# It never falls back to another venue: a line drawn on Coinbase prices can be crossed by a
# Binance tick that Coinbase never printed.
#
# Feed split. Exchange-listed crypto: the venue is the primary feed and
# TradingView the cross-check, so ``backup`` is the same venue read through
# ccxt. CRYPTOCAP / TVC / SP / CME_MINI and the equity, ETF, FX and index
# prefixes are TradingView-only: ``backup`` is ``NO_BACKUP`` and ``tick_size``
# is None (TradingView's own symbol info is the only source for it).
#
# How the table was built, so it can be re-checked:
#   - Crypto defaults to BINANCE:<T>USDT when Binance has an active spot market.
#   - Crypto Binance does not list goes to the first venue that does, in the
#     order COINBASE, KRAKEN, BYBIT, OKX, KUCOIN, GATEIO, MEXC — AFTER a price
#     cross-check against Carrac's own series (and Hyperliquid for HL coins),
#     because a ticker can name two different coins on two venues. Kraken's LIT
#     trades at 0.15 while Hyperliquid's (Carrac's) LIT is 5.18, so LIT is on
#     Bybit; ICX is on KuCoin because Bybit's book was 30 % off the others.
#   - Equities/ETFs: the listing exchange Yahoo reports (NMS/NGM/NCM -> NASDAQ,
#     NYQ -> NYSE, PCX -> AMEX, BTS -> CBOE, PNK -> OTC, GER -> XETR).
#   - Everything else is TradingView's canonical symbol for the instrument the
#     ticker names.

TV_UNMAPPABLE = {
    # Derived ratios: Carrac computes them; TradingView has no single symbol.
    'ALTBTC': 'derived ratio (TOTAL3 - stablecoins) / BTC computed by Carrac; '
              'no single TradingView symbol',
    'TOTAL3BTC': 'derived ratio TOTAL3 / BTC computed by Carrac; no single '
                 'TradingView symbol',
    # Hyperliquid-only: no spot market on any ccxt venue checked.
    'KPEPE': 'Hyperliquid-only 1000x PEPE perp; no spot market on any venue, '
             'so no TradingView spot symbol',
    'KBONK': 'Hyperliquid-only 1000x BONK perp; no spot market on any venue, '
             'so no TradingView spot symbol',
    # BUTTHOLE and KIP have no spot market on any of 9 venues; they are
    # retired via LIFECYCLE_OVERRIDES, so they are outside the universe and
    # need no TradingView answer.
    # Identity conflict — a decision is needed before it can map.
    'BEAM': 'identity conflict: Coinbase/Kraken/Bybit/Bitget BEAM trade at '
            '~0.00198, Carrac series and MEXC BEAM at ~0.0126 — two different '
            'coins share the ticker (2026-09-23)',
}

_TV_SYMBOL_EXPLICIT = {
    # --- Crypto charted off a non-Binance venue -----------------------------
    'LTC': 'COINBASE:LTCUSD',          # example: charted on Coinbase
    # --- Crypto Binance does not list (venue picked per the rule above) -----
    'AKT': 'COINBASE:AKTUSD',
    'DEEP': 'COINBASE:DEEPUSD',
    'FARTCOIN': 'COINBASE:FARTCOINUSD',
    'GRASS': 'COINBASE:GRASSUSD',
    'HYPE': 'COINBASE:HYPEUSD',
    'MON': 'COINBASE:MONUSD',
    'PRIME': 'COINBASE:PRIMEUSD',
    'STORJ': 'COINBASE:STORJUSD',
    'VVV': 'COINBASE:VVVUSD',
    'ARC': 'KRAKEN:ARCUSD',
    'CC': 'KRAKEN:CCUSD',
    'CHEX': 'KRAKEN:CHEXUSD',
    'GRIFFAIN': 'KRAKEN:GRIFFAINUSD',
    'WBT': 'KRAKEN:WBTUSD',
    'XMR': 'KRAKEN:XMRUSD',
    'BRETT': 'BYBIT:BRETTUSDT',
    'CHILLGUY': 'BYBIT:CHILLGUYUSDT',
    'LIT': 'BYBIT:LITUSDT',            # Kraken's LIT is a different coin
    'DSYNC': 'KUCOIN:DSYNCUSDT',
    'ICX': 'KUCOIN:ICXUSDT',
    'MLC': 'GATEIO:MLCUSDT',
    'PIPPIN': 'GATEIO:PIPPINUSDT',
    'RDNT': 'GATEIO:RDNTUSDT',
    'PURR': 'MEXC:PURRUSDT',
    # --- Cross pairs that are real Binance markets --------------------------
    'ETHBTC': 'BINANCE:ETHBTC',
    'XRPBTC': 'BINANCE:XRPBTC',
    'AVAXBTC': 'BINANCE:AVAXBTC',
    # --- Crypto aggregates (TradingView-only) --------------------------------
    'TOTAL': 'CRYPTOCAP:TOTAL', 'TOTAL2': 'CRYPTOCAP:TOTAL2',
    'TOTAL3': 'CRYPTOCAP:TOTAL3', 'BTC.D': 'CRYPTOCAP:BTC.D',
    'USDT.D': 'CRYPTOCAP:USDT.D', 'OTHERS.D': 'CRYPTOCAP:OTHERS.D',
    # --- Indices -------------------------------------------------------------
    'SPX': 'SP:SPX', 'NDX': 'NASDAQ:NDX', 'DJI': 'DJ:DJI', 'RUT': 'TVC:RUT',
    'VIX': 'CBOE:VIX', 'DAX': 'XETR:DAX', 'FTSE': 'TVC:UKX',
    'CAC': 'EURONEXT:PX1', 'BUX': 'BET:BUX', 'NIKKEI': 'TVC:NI225',
    'HSI': 'TVC:HSI', 'HSTECH': 'HSI:HSTECH', 'KOSPI': 'KRX:KOSPI',
    'US10Y': 'TVC:US10Y', 'US30Y': 'TVC:US30Y', 'US5Y': 'TVC:US05Y',
    'ES': 'CME_MINI:ES1!',             # a watchlist symbol; not a Carrac series
    # --- Commodities ---------------------------------------------------------
    'GOLD': 'TVC:GOLD', 'SILVER': 'TVC:SILVER', 'PLATINUM': 'TVC:PLATINUM',
    'OIL': 'TVC:USOIL', 'COPPER': 'COMEX:HG1!', 'NATGAS': 'NYMEX:NG1!',
    'CORN': 'CBOT:ZC1!', 'WHEAT': 'CBOT:ZW1!', 'SOYBEANS': 'CBOT:ZS1!',
    # --- FX ------------------------------------------------------------------
    'DXY': 'TVC:DXY', 'EURUSD': 'FX:EURUSD', 'GBPUSD': 'FX:GBPUSD',
    'USDJPY': 'FX:USDJPY', 'AUDUSD': 'FX:AUDUSD', 'USDCAD': 'FX:USDCAD',
    'USDCHF': 'FX:USDCHF', 'NZDUSD': 'FX:NZDUSD',
    'EURHUF': 'FX_IDC:EURHUF', 'USDHUF': 'FX_IDC:USDHUF',
    # --- Equities (listing exchange per Yahoo) ------------------
    'AAPL': 'NASDAQ:AAPL', 'GOOG': 'NASDAQ:GOOG', 'AMZN': 'NASDAQ:AMZN',
    'META': 'NASDAQ:META', 'TSLA': 'NASDAQ:TSLA', 'NVDA': 'NASDAQ:NVDA',
    'MSFT': 'NASDAQ:MSFT', 'AMD': 'NASDAQ:AMD', 'INTC': 'NASDAQ:INTC',
    'QCOM': 'NASDAQ:QCOM', 'HON': 'NASDAQ:HON', 'WMT': 'NASDAQ:WMT',
    'FMNB': 'NASDAQ:FMNB', 'MSTR': 'NASDAQ:MSTR', 'COIN': 'NASDAQ:COIN',
    'RIOT': 'NASDAQ:RIOT', 'MARA': 'NASDAQ:MARA', 'HOOD': 'NASDAQ:HOOD',
    'SOFI': 'NASDAQ:SOFI', 'PYPL': 'NASDAQ:PYPL', 'PLTR': 'NASDAQ:PLTR',
    'ARM': 'NASDAQ:ARM', 'SMCI': 'NASDAQ:SMCI', 'CRWD': 'NASDAQ:CRWD',
    'NFLX': 'NASDAQ:NFLX',
    'BA': 'NYSE:BA', 'LMT': 'NYSE:LMT', 'RTX': 'NYSE:RTX', 'NOC': 'NYSE:NOC',
    'GD': 'NYSE:GD', 'XOM': 'NYSE:XOM', 'CVX': 'NYSE:CVX', 'JPM': 'NYSE:JPM',
    'GS': 'NYSE:GS', 'V': 'NYSE:V', 'MA': 'NYSE:MA', 'UNH': 'NYSE:UNH',
    'JNJ': 'NYSE:JNJ', 'PG': 'NYSE:PG', 'KO': 'NYSE:KO',
    'AM': 'NYSE:AM', 'NET': 'NYSE:NET', 'SNOW': 'NYSE:SNOW', 'DIS': 'NYSE:DIS',
    'UBER': 'NYSE:UBER', 'LLY': 'NYSE:LLY', 'PFE': 'NYSE:PFE', 'TNK': 'NYSE:TNK',
    'SQ': 'NYSE:XYZ',                  # Block renamed its ticker SQ -> XYZ (2025)
    'RHM': 'XETR:RHM',
    'BYDDY': 'OTC:BYDDY',              # the ADR; stooq routes BYDDY to 1211.HK
    # --- ETFs ----------------------------------------------------------------
    'QQQ': 'NASDAQ:QQQ', 'TLT': 'NASDAQ:TLT', 'SHY': 'NASDAQ:SHY',
    'IBIT': 'NASDAQ:IBIT', 'SOXX': 'NASDAQ:SOXX', 'SMH': 'NASDAQ:SMH',
    'VTI': 'AMEX:VTI', 'IWM': 'AMEX:IWM', 'SPY': 'AMEX:SPY', 'XLE': 'AMEX:XLE',
    'XLF': 'AMEX:XLF', 'XLK': 'AMEX:XLK', 'XLV': 'AMEX:XLV', 'XLP': 'AMEX:XLP',
    'XLI': 'AMEX:XLI', 'EEM': 'AMEX:EEM', 'EWJ': 'AMEX:EWJ', 'EWZ': 'AMEX:EWZ',
    'FXI': 'AMEX:FXI', 'KWEB': 'AMEX:KWEB', 'GDX': 'AMEX:GDX', 'URA': 'AMEX:URA',
    'HYG': 'AMEX:HYG', 'LQD': 'AMEX:LQD', 'BITO': 'AMEX:BITO', 'XBI': 'AMEX:XBI',
    'VNQ': 'AMEX:VNQ',
    'MAGS': 'CBOE:MAGS', 'ARKK': 'CBOE:ARKK', 'IGV': 'CBOE:IGV',
}

# TradingView exchange prefix -> ccxt exchange id, and the quote currency the
# prefix is used with when ``to_tv_symbol`` is asked for a specific venue.
_TV_CRYPTO_VENUES = {
    'BINANCE': ('binance', 'USDT'),
    'COINBASE': ('coinbase', 'USD'),
    'KRAKEN': ('kraken', 'USD'),
    'BYBIT': ('bybit', 'USDT'),
    'OKX': ('okx', 'USDT'),
    'KUCOIN': ('kucoin', 'USDT'),
    'GATEIO': ('gate', 'USDT'),
    'MEXC': ('mexc', 'USDT'),
    'BITGET': ('bitget', 'USDT'),
}
# Longest first, so 'USDT' is tried before 'USD'.
_TV_QUOTES = ('USDT', 'USDC', 'USD', 'EUR', 'BTC', 'ETH')
_USD_LIKE = frozenset({'USDT', 'USDC', 'USD'})

NO_BACKUP = 'no_backup'


class BackupFeed(NamedTuple):
    """A feed Carrac can read for the same instrument: ccxt exchange id + the
    pair as that venue names it (Coinbase ``LTC-USD``, Binance ``LTCUSDT``)."""
    exchange: str
    pair: str


@dataclass(frozen=True)
class TvInstrument:
    """One resolved TradingView chart symbol."""
    ticker: str                 # Carrac ticker
    tv_symbol: str              # e.g. 'COINBASE:LTCUSD'
    exchange: str               # TradingView prefix, e.g. 'COINBASE'
    venue_symbol: str           # pair as the venue names it, e.g. 'LTC-USD'
    tick_size: Optional[float]  # venue price tick; None for TradingView-only
    backup: object              # BackupFeed, or NO_BACKUP


class TvSymbolError(ValueError):
    """Base for every TradingView-map refusal."""


class UnsupportedVenueError(TvSymbolError):
    """The chart's venue cannot be read (unknown prefix, or no ccxt market for
    the pair there). A watch must refuse — never read another venue instead."""


class UnmappedSymbolError(TvSymbolError):
    """The ticker (or TradingView-only symbol) is not in the map."""


def _build_tv_symbol_map() -> Dict[str, str]:
    mapping = dict(_TV_SYMBOL_EXPLICIT)
    for t in _CRYPTO_SYMBOLS:
        if t in mapping or t in TV_UNMAPPABLE or is_delisted(t):
            continue
        mapping[t] = f'BINANCE:{t}USDT'
    return mapping


_TV_SYMBOL_MAP = _build_tv_symbol_map()
_TV_REVERSE = {tv: t for t, tv in _TV_SYMBOL_MAP.items()}


def tv_symbol_map() -> Dict[str, str]:
    """A copy of the whole Carrac ticker -> TradingView symbol table."""
    return dict(_TV_SYMBOL_MAP)


_CCXT_MARKETS_CACHE: Dict[str, dict] = {}


def _ccxt_markets(ccxt_id: str) -> dict:
    """Live ccxt spot markets for one venue: unified symbol -> {id, tick}.

    Default ``markets`` loader. ccxt is imported here, not at module scope, so
    the bare-host cron can still import this module (see module docstring).
    Cached for the process lifetime; one ``load_markets`` per venue.
    """
    if ccxt_id not in _CCXT_MARKETS_CACHE:
        import ccxt
        ex = getattr(ccxt, ccxt_id)()
        out = {}
        for sym, m in ex.load_markets().items():
            if not m.get('spot') or m.get('active') is False:
                continue
            tick = (m.get('precision') or {}).get('price')
            if tick is not None and ex.precisionMode == ccxt.DECIMAL_PLACES:
                tick = 10 ** -tick
            out[sym] = {'id': m['id'], 'tick': tick}
        _CCXT_MARKETS_CACHE[ccxt_id] = out
    return _CCXT_MARKETS_CACHE[ccxt_id]


def from_tv_symbol(tv_symbol: str, markets=None) -> TvInstrument:
    """Resolve a TradingView chart symbol to the instrument Carrac watches.

    ``markets`` is ``callable(ccxt_id) -> {unified: {'id', 'tick'}}``; default
    reads ccxt live. Raises ``UnsupportedVenueError`` when the venue cannot be
    read and ``UnmappedSymbolError`` for a TradingView-only symbol not in the
    map — never a fallback to a different venue.
    """
    markets = markets or _ccxt_markets
    tv = (tv_symbol or '').strip().upper()
    prefix, sep, name = tv.partition(':')
    if not sep or not prefix or not name:
        raise TvSymbolError(f'{tv_symbol!r} is not an EXCHANGE:SYMBOL TradingView symbol')

    if prefix in _TV_CRYPTO_VENUES:
        ccxt_id, _ = _TV_CRYPTO_VENUES[prefix]
        if name.endswith('.P'):
            raise UnsupportedVenueError(
                f'{tv}: perpetual contracts are not supported, only spot')
        quote = next((q for q in _TV_QUOTES
                      if name.endswith(q) and len(name) > len(q)), None)
        if quote is None:
            raise UnsupportedVenueError(f'{tv}: cannot split {name} into base/quote')
        base = name[:-len(quote)]
        unified = f'{base}/{quote}'
        market = markets(ccxt_id).get(unified)
        if market is None:
            raise UnsupportedVenueError(
                f'{tv}: {ccxt_id} has no active {unified} spot market — refusing '
                f'rather than reading another venue')
        ticker = _TV_REVERSE.get(tv) or (base if quote in _USD_LIKE else base + quote)
        return TvInstrument(ticker=ticker, tv_symbol=tv, exchange=prefix,
                            venue_symbol=market['id'], tick_size=market['tick'],
                            backup=BackupFeed(exchange=ccxt_id, pair=market['id']))

    if tv in _TV_REVERSE:
        return TvInstrument(ticker=_TV_REVERSE[tv], tv_symbol=tv, exchange=prefix,
                            venue_symbol=name, tick_size=None, backup=NO_BACKUP)
    if any(v.startswith(prefix + ':') for v in _TV_REVERSE):
        raise UnmappedSymbolError(f'{tv} is a TradingView-only symbol not in the map')
    raise UnsupportedVenueError(f'{tv}: unknown venue {prefix} — Carrac cannot read it')


def to_tv_symbol(ticker: str, venue: Optional[str] = None, markets=None) -> str:
    """The TradingView symbol for a Carrac ticker.

    With ``venue`` (a TradingView prefix such as 'COINBASE') the symbol on that
    venue is returned, after checking the venue really lists the pair — an
    unreadable venue raises ``UnsupportedVenueError``.
    """
    t = (ticker or '').strip().upper()
    canonical = _TV_SYMBOL_MAP.get(t)
    if venue is None:
        if canonical:
            return canonical
        if t in TV_UNMAPPABLE:
            raise UnmappedSymbolError(f'{t}: {TV_UNMAPPABLE[t]}')
        raise UnmappedSymbolError(f'{t} has no TradingView symbol in the map')

    v = venue.strip().upper()
    if canonical and canonical.startswith(v + ':'):
        return canonical
    if v not in _TV_CRYPTO_VENUES:
        raise UnsupportedVenueError(f'{t} on {v}: unknown venue — Carrac cannot read it')
    tv = f'{v}:{t}{_TV_CRYPTO_VENUES[v][1]}'
    from_tv_symbol(tv, markets=markets)  # raises if the venue lacks the pair
    return tv
