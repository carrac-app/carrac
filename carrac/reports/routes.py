"""
Report routes: morning report page, save/load/archive, weekly digest.

All endpoints are served at their
original paths via explicit rules.
"""

from carrac.site_url import site_base_url
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import (
    Blueprint, render_template, jsonify, redirect, request, session,
    current_app, send_file, Response, g,
)

import push_notifications
from carrac.charts.ratios import _build_ratio_df
from carrac.reports.brief_archive import CSP_NONCE_PLACEHOLDER
from carrac.auth.decorators import (
    login_required, auth_or_key_required, verify_admin_key,
)
from carrac.extensions import csrf, limiter
from carrac.notifications.telegram import send_telegram_message
from user_db import user_db


def _ntfy_base() -> str:
    """Operator ntfy base URL — single source of truth.

    Imported rather than re-declared so this cannot drift from
    carrac/notifications/ntfy.py the way the scripts/ copies did.
    """
    from carrac.notifications.ntfy import NTFY_BASE
    return NTFY_BASE


logger = logging.getLogger('carrac.reports')


# Cap on the assembled briefing text. Briefings run tens of KB
# (facts/calendar/quant/chart/sentiment context); 300KB is a generous ceiling
# well above any real briefing, same idiom as the other size caps in this
# file (content 100KB, metadata 500KB, audio_script 50KB). Unlike those, an
# oversized briefing does NOT reject the save — the published report content
# is the thing that matters; the debug-only briefing is just dropped with a
# log line so a pathological briefing can never block a report from saving.
_MAX_SOURCE_BRIEFING_CHARS = 300_000


def _extract_source_briefing(data: dict, label: str):
    """Pull an optional source_briefing out of a save-report
    payload, dropping (not rejecting) it if oversized. SHARED reports only —
    callers here are morning/afternoon/weekly/week_ahead/general; personal
    sections never pass through this (see save_personal_section)."""
    briefing = data.get('source_briefing')
    if not briefing:
        return None
    if not isinstance(briefing, str):
        return None
    if len(briefing) > _MAX_SOURCE_BRIEFING_CHARS:
        logger.warning(
            "%s: source_briefing too large (%d chars, max %d) — dropping, report content unaffected",
            label, len(briefing), _MAX_SOURCE_BRIEFING_CHARS,
        )
        return None
    return briefing


def _use_editorial(request, is_admin):
    # ?classic=1 escape hatch: lets any member reach the classic layout,
    # flag state aside.
    if request.args.get("classic") == "1":
        return False
    if os.environ.get("REPORT_EDITORIAL_ENABLED", "").lower() in ("1", "true", "yes"):
        return True
    if is_admin and request.args.get("editorial") == "1":
        return True
    return False


# ------------------------------------------------------------------
# Derivatives Desk (Tier 2.2) — flag/tier gating.
#
# Split into three small, independently-testable pieces (see
# tests/test_desk_gating.py) rather than inlined in _editorial_context()
# like the other single-line band gathers, because this band has TWO
# separate gates (is it shown at all? is it shown UNLOCKED?) instead of one.
# The NO-LEAK RULE itself — redacting values when locked — lives entirely in
# the BUILDER (carrac/facts/bands.py::build_desk); none of this trio ever
# touches a row value.
# ------------------------------------------------------------------

def _desk_enabled(is_admin):
    """Whether the Derivatives Desk band is gathered at all this request.

    Same shape as `_use_editorial()`: an env-var kill switch (checked fresh
    per request — flipping `DERIVATIVES_DESK_ENABLED` needs a compose
    RECREATE, not just `docker restart`, since env_file is only re-read on
    recreate) OR an admin override (unconditional — no `?editorial=1`-style
    query param needed; an admin should see the band the moment the code
    ships, as a live preview, before the flag is flipped for everyone).
    Flag stays OFF at ship at first — admin-only preview.
    """
    if os.environ.get("DERIVATIVES_DESK_ENABLED", "").lower() in ("1", "true", "yes"):
        return True
    return bool(is_admin)


def _desk_unlocked(is_admin, tier):
    """Whether the Derivatives Desk should render REAL values for this
    caller (vs. the server-redacted locked teaser).

    The SAME discriminator the personal-report splice uses — see
    carrac/fastapi_app/routers/reports.py::get_latest_morning_report's
    `serves_personal` — no new tier vocabulary: any tier whose
    `personal_report` limit isn't 'none' unlocks it. An admin always
    unlocks it outright (operator preview), independent of tier.

    This is a ROUTE-layer decision — carrac/facts/bands.py::build_desk
    trusts the `unlocked` value it's given and never re-derives tier
    itself (see that function's docstring).
    """
    if is_admin:
        return True
    from carrac.auth.decorators import get_tier_limits
    limits = get_tier_limits(tier)
    return bool(limits.get('personal_report') and limits.get('personal_report') != 'none')


def _gather_desk(is_admin, tier):
    """Derivatives Desk gather for `_editorial_context()`: None when
    `_desk_enabled()` says not to show it at all; otherwise defers to
    `carrac.facts.bands.build_desk()` (which never raises) with `unlocked`
    resolved via `_desk_unlocked()`. Non-fatal: any exception here (e.g. a
    bad tier lookup) also yields None, same contract as every other
    build_* band consumed by _editorial_context().
    """
    try:
        if not _desk_enabled(is_admin):
            return None
        from carrac.facts.bands import build_desk
        return build_desk(unlocked=_desk_unlocked(is_admin, tier))
    except Exception:
        return None


# Editorial v4 (linear desk note): one spotlight slot per registry category,
# ordered Intermarket → Equities → Commodities → Crypto. Display labels map
# the registry key to a reader-facing section name; unmapped keys use the key
# itself (Equities / Commodities / Crypto all read fine as-is).
_CLUSTER_ORDER = ["Intermarket", "Equities", "Commodities", "Crypto"]
_CLUSTER_LABELS = {"Intermarket": "Macro & FX"}


def _pick_cluster_spotlight(cats, bundles, salience):
    """One spotlight slot per cluster, highest salience within; empty cluster skipped."""
    picks = []
    for cluster in _CLUSTER_ORDER:
        best = None
        for sym in cats.get(cluster, []):
            if sym not in bundles:
                continue
            s = salience.get(sym, 0.0)
            if best is None or s > best[0]:
                best = (s, sym)
        if best:
            picks.append((cluster, best[1], bundles[best[1]]))
    return picks

reports_bp = Blueprint('reports', __name__)


# ------------------------------------------------------------------
# Late-import helpers (avoid circular deps at module load time)
# ------------------------------------------------------------------

def _get_db():
    """Return the shared OHLCVDatabase instance."""
    from carrac.services import get_services
    return get_services().db


def _get_consensio():
    """Return the shared ConsensioCalculator instance."""
    from carrac.services import get_services
    return get_services().consensio


def _get_cached_crypto_prices():
    """Late import wrapper around cache_manager.get_cached_crypto_prices."""
    from cache_manager import get_cached_crypto_prices
    return get_cached_crypto_prices()


def _viewer_is_operator():
    """True when this request may see the operator's private report text.

    The admin tier: the operator's session or own API key, and the internal
    key the report engine reads its past reports with. ``?as=general`` asks
    for the shared cut and can only lower what is shown.
    """
    from carrac.auth.decorators import get_request_tier
    return get_request_tier() == 'admin' and request.args.get('as') != 'general'


def _text_for_viewer(report, report_type):
    """``report_text_for_viewer`` for the current request."""
    from carrac.reports.sanitize import report_text_for_viewer
    return report_text_for_viewer(report, report_type, operator=_viewer_is_operator())


# ------------------------------------------------------------------
# Ratio helpers
# ------------------------------------------------------------------

def _live_price_for_delta(asset, crypto_prices, database):
    """Current price for one asset in the report delta header, or None.

    Ratio symbols (``GOLD_BTC``, ``QQQ_SPX`` ...) are Carrac's own composites:
    the report priced them through the ratio builder, so the live side must
    too. Handing the combined token to get_ohlcv_data trips the equity guard
    on every report view and the row is silently dropped.
    """
    cp_entry = (crypto_prices or {}).get(asset)
    cp = cp_entry.get('price') if isinstance(cp_entry, dict) else cp_entry
    if cp is not None:
        return cp
    try:
        if '_' in asset:
            num_sym, den_sym = asset.split('_', 1)
            df = _build_ratio_df(num_sym, den_sym, '1D')
        elif database:
            df = database.get_ohlcv_data(asset, limit=1)
        else:
            return None
        if df is not None and not df.empty:
            return float(df['close'].iloc[-1])
    except Exception as exc:
        logger.debug("delta header: no live price for %s: %s", asset, exc)
    return None


def _compute_ratio_consensio(symbol: str) -> dict | None:
    """Compute consensio for a ratio symbol like BTC_SPX, with prev-day change."""
    parts = symbol.split('_')
    if len(parts) != 2:
        return None
    num_sym, den_sym = parts
    try:
        consensio = _get_consensio()
        df_1d = _build_ratio_df(num_sym, den_sym, '1D')
        df_3d = _build_ratio_df(num_sym, den_sym, '3D')
        df_1w = _build_ratio_df(num_sym, den_sym, '1W')
        if df_1d.empty:
            return None
        result = consensio.calculate_multi_timeframe(df_1d, df_3d, df_1w)
        entry = {
            '1D': result['1D']['signal'],
            '3D': result['3D']['signal'],
            '1W': result['1W']['signal'],
            'total': result['total']['positions'],
        }
        # Compute prev-day signals using data without last row
        prev_1d = df_1d.iloc[:-1] if len(df_1d) > 1 else df_1d
        prev_3d = df_3d.iloc[:-1] if len(df_3d) > 1 else df_3d
        prev_1w = df_1w.iloc[:-1] if len(df_1w) > 1 else df_1w
        prev_result = consensio.calculate_multi_timeframe(prev_1d, prev_3d, prev_1w)
        entry['prev_1D'] = prev_result['1D']['signal']
        entry['prev_3D'] = prev_result['3D']['signal']
        entry['prev_1W'] = prev_result['1W']['signal']
        entry['prev_total'] = prev_result['total']['positions']
        return entry
    except Exception:
        return None


# ------------------------------------------------------------------
# Pulse Cockpit (additive, read-only, Flask-only — not in MIGRATED_PATHS)
# ------------------------------------------------------------------

@reports_bp.route('/report/pulse')
@login_required
def pulse_report():
    """Fast market cockpit — serves a worker-precomputed payload from cache.

    The background worker rebuilds the payload every ~30 min (see
    precompute._precompute_loop); reading it here is a ~50ms Redis hit
    instead of a multi-second live rebuild (build_snapshot + RS rankings).
    Falls back to an inline build only when the cache is cold (e.g. right
    after a worker restart, before the first warm cycle).
    """
    from carrac.precompute import response_cache
    pulse = response_cache.get('pulse_payload')
    if pulse is None:
        from carrac.reports.pulse import build_pulse_payload
        pulse = build_pulse_payload()
    return render_template('pulse_report.html', pulse=pulse)


# Morning Report Page
# ------------------------------------------------------------------

# Asset list for the morning report board — ordered by user preference.
# Module-level so the route (template metadata) and the worker pre-warm
# (build_morning_board) share one source of truth.
MORNING_ASSETS = [
    {'symbol': 'SPX', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'QQQ', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'BTC', 'timeframes': ['4H', '1D', '3D', '1W', '1M']},
    {'symbol': 'GOLD', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'SILVER', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'OIL', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'MAGS', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'COPPER', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'NATGAS', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'XLE', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'CORN', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'WHEAT', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'SOYBEANS', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'ETH', 'timeframes': ['4H', '1D', '3D', '1W', '1M']},
    {'symbol': 'XRP', 'timeframes': ['4H', '1D', '3D', '1W', '1M']},
    {'symbol': 'TOTAL', 'timeframes': ['1D', '3D', '1W', '1M']},
    {'symbol': 'DXY', 'timeframes': ['1D', '3D', '1W', '1M'], 'ratio_group': True, 'text_only': True},
    {'symbol': 'BTC_SPX', 'timeframes': ['1D', '3D', '1W', '1M'], 'ratio': True, 'ratio_group': True},
    {'symbol': 'GOLD_BTC', 'timeframes': ['1D', '3D', '1W', '1M'], 'ratio': True, 'ratio_group': True},
    {'symbol': 'SPX_GOLD', 'timeframes': ['1D', '3D', '1W', '1M'], 'ratio': True, 'ratio_group': True},
    {'symbol': 'ETH_BTC', 'timeframes': ['1D', '3D', '1W', '1M'], 'ratio': True, 'ratio_group': True},
    {'symbol': 'XRP_BTC', 'timeframes': ['1D', '3D', '1W', '1M'], 'ratio': True, 'ratio_group': True},
    {'symbol': 'QQQ_SPX', 'timeframes': ['1D', '3D', '1W', '1M'], 'ratio': True, 'ratio_group': True},
    {'symbol': 'GOLD_QQQ', 'timeframes': ['1D', '3D', '1W', '1M'], 'ratio': True, 'ratio_group': True},
    {'symbol': 'MAGS_SPX', 'timeframes': ['1D', '3D', '1W', '1M'], 'ratio': True, 'ratio_group': True},
]


def _board_beacon(msg: str) -> None:
    """Send one board alarm to Sentry/Bugsink. Seam so the alarm is testable."""
    try:
        import sentry_sdk
        sentry_sdk.capture_message(msg, level='error')
    except Exception:
        pass


def build_morning_board() -> dict:
    """Compute the morning report's prices + consensio board.

    User-independent (no session) → cacheable. The background worker
    rebuilds this every ~30 min and stores it under 'morning_board' so the
    page reads a warm cache instead of running ~24 multi-timeframe DB
    fetches + consensio calculations on every request (the ~9s bottleneck).

    Returns {'prices': {sym: float}, 'consensio': {sym: {...signals}}}.
    """
    db = _get_db()
    consensio = _get_consensio()

    # Never raise (a throw 500s the page on the cold-cache fallback path), but
    # never swallow a whole-body failure either: wrapping everything in a bare
    # `except Exception: pass` would let a cache-layer or DB failure blank the
    # entire prices/consensio dict, the worker would cache the empty board as a
    # successful warm, and the report page + public /brief would render
    # without data for the cache TTL, unlogged. Degrade, but alarm.
    # Per-symbol failures stay tolerated; a BLANKED dict does not.

    # Current prices — crypto from realtime cache, others from last daily close
    prices = {}
    try:
        cached = _get_cached_crypto_prices()
    except Exception:
        logger.error("morning board: crypto price cache read failed", exc_info=True)
        cached = {}
    # Ratio entries (BTC_SPX, GOLD_BTC, ...) must be
    # priced through the same ratio machinery as the consensio loop below
    # and the delta header (_live_price_for_delta). Handing the combined
    # token to db.get_ohlcv_data trips the equity-path refusal guard on
    # every worker cycle (~30 min) instead of ever pricing the row.
    for a in MORNING_ASSETS:
        sym = a['symbol']
        if sym not in prices:
            try:
                price = _live_price_for_delta(sym, cached, db)
            except Exception:
                price = None
                logger.debug("morning board: price fetch failed for %s", sym, exc_info=True)
            if price is not None:
                prices[sym] = price

    # Consensio signals per asset (including ratios) with prev-day change
    consensio_data = {}
    prev_snapshot = {}
    try:
        prev_snapshot = db.load_previous_snapshot()
    except Exception:
        # Degrades only the prev-day change column, not the board itself.
        logger.warning("morning board: previous snapshot unavailable", exc_info=True)
    for a in MORNING_ASSETS:
        sym = a['symbol']
        try:
            if a.get('ratio'):
                rc = _compute_ratio_consensio(sym)
                if rc:
                    consensio_data[sym] = rc
            else:
                data = db.get_multi_timeframe_data(sym)
                if data['1D'].empty:
                    continue
                result = consensio.calculate_multi_timeframe(data['1D'], data['3D'], data['1W'])
                entry = {
                    '1D': result['1D']['signal'],
                    '3D': result['3D']['signal'],
                    '1W': result['1W']['signal'],
                    'total': result['total']['positions'],
                }
                # Attach previous day's signals from snapshot
                prev = prev_snapshot.get(sym, {})
                if prev:
                    entry['prev_1D'] = prev.get('signal_1d')
                    entry['prev_3D'] = prev.get('signal_3d')
                    entry['prev_1W'] = prev.get('signal_1w')
                    entry['prev_total'] = prev.get('positions')
                consensio_data[sym] = entry
        except Exception:
            logger.debug("morning board: consensio failed for %s", sym, exc_info=True)

    if not prices or not consensio_data:
        msg = (f"morning board built BLANK (prices={len(prices)} "
               f"consensio={len(consensio_data)}) — the report page and the "
               f"public /brief will render without this data, and the worker "
               f"caches the blank for up to 30 minutes")
        logger.error(msg)
        _board_beacon(msg)

    return {'prices': prices, 'consensio': consensio_data}


def _editorial_context(conf_needed: bool = True, is_admin: bool = False, tier: str = 'free') -> dict:
    """Gather all editorial-only report context (clusters/board/heat + R1/R2
    side-modules + bands) for the editorial v4 page.

    Pure refactor: this is the exact former inline ``if editorial:`` gather
    block from ``morning_report()``, extracted verbatim so the public
    ``/brief`` route (Tier 2.1) can reuse the identical gather without
    duplicating it. Each sub-value gathers independently and is left at its
    safe default (``None``/``[]``/``{}``) on any failure, so callers never see
    a partial-page crash from one failing data source.

    ``conf_needed``: when False, skips the confluence-map fetch (used by a
    future caller that doesn't need ladder/wall data); defaults to True to
    match the existing ``/report/morning?editorial=1`` behavior.

    ``is_admin``/``tier`` (Tier 2.2): gate the Derivatives Desk band only
    (see ``_gather_desk()``) — defaults match ``/brief``'s anonymous
    context (``is_admin=False``, ``tier='free'``) so a caller that forgets
    to pass them explicitly gets the SAFE (locked-or-absent) behavior, never
    an accidental unlock. ``morning_report()`` passes its real ``is_admin``/
    ``tier`` explicitly.
    """
    # Gather facts bundle data for the editorial Assets pane (non-fatal).
    # Browser cannot read facts:bundle:* (API-key auth), so the pane is server-rendered.
    # facts_list stays [] on this route (editorial v4): the cluster spotlight below
    # replaces the flat fcard list. /assets/<symbol> builds its own facts_list and
    # the partial keeps full back-compat with it.
    facts_list = []
    heat = None
    # Cluster spotlight (one pick per registry category) + cross-asset board
    # (registry-driven; built below). cards stays [] — superseded by clusters.
    cards = []
    clusters = []
    board = []
    # Side-module variables — each gathers independently; None when unavailable.
    correlations = None
    cot = None
    pulse = None
    macro = None
    regime = None
    quant_by_symbol = {}
    quant_anomalies = None
    # R1 editorial data bands (carrac/facts/bands.py) — deterministic, no LLM
    # involvement (bulletproof rule). Each gathers independently; None/[] when
    # unavailable. regime_clock deliberately reads the SAME quant.signals
    # regime_hmm series as `regime` above (see bands.py docstring) so it can
    # never describe a different classifier than the banner it augments.
    docket = None
    liquidity = None
    breadth = None
    regime_clock = None
    # R2 editorial data bands (carrac/facts/bands.py) — same deterministic,
    # no-LLM contract as R1. rotation reads the cached RS/RRG payload
    # (carrac.caches.get_cached_rs) — never recomputes _compute_rs_rankings()
    # per-request (see bands.py docstring).
    rotation = None
    flows = None
    # Confluence map: fetch + parse the whole scanner blob ONCE for the page, then
    # build every ladder/wall from it (per-symbol reads re-parse the blob ~1.3s each).
    _conf: dict = {}
    if conf_needed:
        try:
            from carrac.scanner.confluence_query import get_confluence_map
            _conf = get_confluence_map()
        except Exception:
            _conf = {}

    # Cluster spotlight (one pick per registry category) + cross-asset board
    # + heat strip — all registry-driven, single gather pass. Non-fatal: any
    # failure leaves clusters/board/heat empty so the page never 500s.
    try:
        from carrac.facts.wire import get_asset_facts
        from carrac.facts.sentences import asset_sentences
        from carrac.facts.edition import spotlight_lede
        from carrac.facts.card import level_flips, state_rows
        from carrac.facts.ladder import ladder, quiet_wall, levels_summary
        from carrac.facts.labels import asset_name
        from carrac.facts.quant_card import quant_rows, market_anomalies
        from carrac.facts.base_rates import get_history_says
        from carrac.assets.registry import categories, ratio_set
        from quant.api.routes import build_snapshot
        from quant.engine.config import TARGET_ASSETS

        _HEAT_GROUPS = [
            ("Crypto", ["BTC", "ETH", "XRP"]),
            ("Equity · Macro", ["SPX", "GOLD"]),
        ]

        _cats = categories()
        _ratios = ratio_set()

        # One gather pass: build the full bundle dict (the comparison set for
        # the comparative ledes) + the salience ranking. Reused by the
        # spotlight, board, and heat strip, so each asset is fetched from
        # cache once.
        _all_bundles: dict[str, dict] = {}
        _ranked = []
        for _members in _cats.values():
            for _sym in _members:
                _b = get_asset_facts(_sym)
                if not _b:
                    continue  # skip assets with no cached bundle
                _all_bundles[_sym] = _b
                # DXY sits in _STATIC_RATIOS (it gates scanner/alert
                # surfaces), but editorially it's the natural Macro & FX
                # spotlight hero — rank it; skip only the true spreads.
                if _sym in _ratios and _sym != "DXY":
                    continue
                _top = asset_sentences(_b, k=1)
                _sal = _top[0].salience if _top else 0.0
                _ranked.append((_sal, _sym, _b))
        _ranked.sort(key=lambda t: t[0], reverse=True)
        _salience_by_sym = {_sym: _sal for _sal, _sym, _ in _ranked}

        # Heat strip — range-percentile rows grouped for the wash chart.
        _heat_groups = []
        for _label, _syms in _HEAT_GROUPS:
            _rows = []
            for _sym in _syms:
                _b = _all_bundles.get(_sym)
                if _b and _b.get('range_pctile') and _b['range_pctile'].get('pct') is not None:
                    _rows.append({'sym': _sym, 'pct': int(_b['range_pctile']['pct'])})
            if _rows:
                _heat_groups.append({'label': _label, 'rows': _rows})
        if _heat_groups:
            heat = _heat_groups

        # Cluster spotlight — one slot per registry category, highest
        # salience within; empty cluster skipped (THE CONTRACT).
        # DXY first among Intermarket candidates: the picker keeps the
        # first-seen on salience ties, and the spreads all tie at 0.0.
        _cats_pick = dict(_cats)
        _im = _cats_pick.get("Intermarket") or []
        if "DXY" in _im:
            _cats_pick["Intermarket"] = ["DXY"] + [s for s in _im if s != "DXY"]
        _picks = _pick_cluster_spotlight(_cats_pick, _all_bundles, _salience_by_sym)
        _clusters = []
        for _cluster, _sym, _b in _picks:
            _quant = []
            if _sym in TARGET_ASSETS:
                try:
                    _snap = build_snapshot(_sym)
                    _quant = quant_rows(_snap, _sym)
                    if _quant:
                        quant_by_symbol[_sym] = _quant
                    # Global items (regime, market anomalies) are the same in
                    # every snapshot — capture once from the first target.
                    if regime is None:
                        regime = (_snap or {}).get("regime")
                    if quant_anomalies is None:
                        quant_anomalies = market_anomalies(_snap)
                except Exception:
                    pass
            _lad = ladder(_conf.get(_sym))
            # History says (R3-A option A) — deterministic
            # per-asset base rate, own engine path off quant.price_daily
            # (see carrac/facts/base_rates.py). Own try/except so one
            # asset's DB hiccup can't drop the whole spotlight loop; runs
            # for ALL viewers (the headline line is free by design).
            _hs = None
            try:
                _hs = get_history_says(_sym)
            except Exception:
                _hs = None
            _clusters.append({
                "category": _cluster,
                "label": _CLUSTER_LABELS.get(_cluster, _cluster),
                "name": asset_name(_sym),
                "ticker": _sym,
                "lede": spotlight_lede(_sym, _all_bundles),
                "b": _b,
                "state_rows": state_rows(_b),
                "flips": level_flips(_b),
                "ladder": _lad,
                "levels_summary": levels_summary(_lad),
                "quant": _quant,
                "history_says": _hs,
            })
        clusters = _clusters

        # Board — breadth: every asset with a bundle, grouped by cluster
        # (Crypto / Equities / Commodities / Intermarket order preserved).
        # note = comparative lede; pct = range percentile (keys the wash tint).
        _board = []
        for _cluster, _members in _cats.items():
            _rows = []
            for _sym in _members:
                _b = _all_bundles.get(_sym)
                if not _b:
                    continue
                _rows.append({
                    "name": asset_name(_sym),
                    "ticker": _sym,
                    "note": spotlight_lede(_sym, _all_bundles),
                    "pct": (_b.get("range_pctile") or {}).get("pct"),
                    "wall": quiet_wall(_conf.get(_sym)),
                })
            if _rows:
                _board.append({"name": _cluster, "assets": _rows})
        board = _board
    except Exception:
        pass  # spotlight/board/heat gracefully empty; rest of page unaffected

    # Correlation shifts — reshape {a,b,prior,today,shift,became_significant}
    # to the partial's expected {pair,was,now,delta,became_significant}.
    try:
        from carrac.facts.correlations import get_correlation_shifts
        _raw_corr = get_correlation_shifts()
        if _raw_corr:
            correlations = [
                {
                    "pair": f"{r['a']} / {r['b']}",
                    "was": r["prior"],
                    "now": r["today"],
                    "delta": r["shift"],
                    "became_significant": r["became_significant"],
                }
                for r in _raw_corr
            ] or None
    except Exception:
        pass

    # COT positioning — top 6 rows (stretched first), reshaped to {label, value}.
    try:
        from quant.api.routes import build_cot
        _cot_data = build_cot()
        if _cot_data and _cot_data.get('series'):
            _series = _cot_data['series']
            _stretched = [r for r in _series if r.get('stretched')]
            _other = sorted(
                [r for r in _series if not r.get('stretched')],
                key=lambda r: abs(r.get('zscore') or 0), reverse=True,
            )
            _cot_rows = []
            for r in (_stretched + _other)[:6]:
                _parts = [
                    f"z {(r.get('zscore') or 0):+.1f}",
                    f"{(r.get('percentile') or 0):.0f}th pct",
                ]
                _wow = r.get('wow_change')
                if _wow is not None:
                    _parts.append(f"WoW {int(round(_wow)):+d}")
                _label = f"{r.get('market_label', '')} · {r.get('type_label', '')}"
                if r.get('stretched'):
                    _label += " ⚠"
                _cot_rows.append({"label": _label, "value": " · ".join(_parts)})
            cot = _cot_rows or None
    except Exception:
        pass

    # Market pulse — from the Redis-cached sentiment (no live HTTP call).
    # Reshapes fear_greed / BTC L/S / MVRV-z / MOVE to [{label, value}].
    try:
        from carrac.caches import get_cached_sentiment, get_stale_sentiment
        _sent_data = get_cached_sentiment() or get_stale_sentiment()
        if _sent_data:
            _sentiment = (_sent_data.get('sentiment') or {})
            _pulse_rows = []
            _fg = _sentiment.get('fear_greed') or {}
            if _fg.get('value') is not None:
                _pulse_rows.append({
                    "label": "Fear & Greed",
                    "value": f"{_fg['value']} ({_fg.get('classification', '')})",
                })
            _ls = (_sentiment.get('long_short') or {}).get('BTC') or {}
            if _ls.get('ratio') is not None:
                _pulse_rows.append({
                    "label": "BTC L/S",
                    "value": f"{_ls['ratio']:.2f} ({_ls.get('shortPct', 0):.0f}% short)",
                })
            _mvrv = _sentiment.get('mvrv_zscore') or {}
            if _mvrv.get('zscore') is not None:
                _pulse_rows.append({
                    "label": "MVRV-z",
                    "value": f"{_mvrv['zscore']:+.2f} ({_mvrv.get('zone', '')})",
                })
            # MOVE bond vol from the quant snapshot in-process cache (best-effort).
            try:
                from quant.api.routes import _qe_cache
                _snap = _qe_cache.get('snapshot:BTC')
                _bv = ((_snap or {}).get('bond_vol') or {})
                if _bv.get('level') is not None and _bv.get('label'):
                    _pulse_rows.append({
                        "label": "MOVE bond vol",
                        "value": f"{_bv['level']:.0f} ({_bv['label']})",
                    })
            except Exception:
                pass
            pulse = _pulse_rows or None
    except Exception:
        pass

    # Macro facts strip — human-readable, facts-only, no markdown markers in output.
    try:
        from carrac.grounding.facts_block import render_macro_facts_strip
        _strip = render_macro_facts_strip()
        if _strip:
            # Strip the header (**Macro facts** ...) and source footer (_source:..._)
            _lines = [
                l for l in _strip.split('\n')
                if l and not l.startswith('**') and not l.startswith('_source')
            ]
            macro = " · ".join(_lines) if _lines else None
    except Exception:
        pass

    # Today's docket — quant.events, yesterday through +7 days. NO consensus/
    # expected column anywhere (bulletproof rule — deterministic, no LLM).
    try:
        from carrac.facts.bands import build_docket
        docket = build_docket()
    except Exception:
        pass

    # Liquidity & credit — curated FRED set (VIX, HY spread, RRP, TGA, M2,
    # jobless claims, breakevens) from quant.macro + quant.zscores.
    try:
        from carrac.facts.bands import build_liquidity
        liquidity = build_liquidity()
    except Exception:
        pass

    # Breadth internals — above-200D/50D %, oversold/overbought, new highs
    # vs lows, advancers vs decliners, from the cached market-breadth scan.
    try:
        from carrac.facts.bands import build_breadth
        breadth = build_breadth()
    except Exception:
        pass

    # Regime clock — day-count + median-run-length suffix for the regime
    # banner above. Reads the SAME quant.signals regime_hmm series as
    # `regime` (set earlier in the cluster-spotlight gather), so it can
    # never drift onto a different classifier than the banner it augments.
    try:
        from carrac.facts.bands import build_regime_clock
        regime_clock = build_regime_clock()
    except Exception:
        pass

    # Rotation strip — RRG quadrant grouping, read from the SAME cache
    # /api/relative-strength serves (never recomputed per-request).
    try:
        from carrac.facts.bands import build_rotation
        rotation = build_rotation()
    except Exception:
        pass

    # Flows & Attention — BTC ETF net flow (day + 5-day), stablecoin
    # dominance/supply, strongest-|z| Google Trends search-interest row.
    try:
        from carrac.facts.bands import build_flows
        flows = build_flows()
    except Exception:
        pass

    # Derivatives Desk (Tier 2.2) — BTC+ETH DVOL/IV-term/skew/basis/GEX/
    # max-pain/put-call/OI/long-short, server-redacted when the caller isn't
    # unlocked. The NO-LEAK RULE lives entirely in the BUILDER (see
    # carrac/facts/bands.py::build_desk's docstring) — this call site only
    # decides ENABLED (flag/admin) and UNLOCKED (tier); see _gather_desk()/
    # _desk_enabled()/_desk_unlocked() above, each independently unit-tested
    # in tests/test_desk_gating.py.
    desk = _gather_desk(is_admin, tier)

    return {
        'facts_list': facts_list,
        'cards': cards,
        'clusters': clusters,
        'board': board,
        'heat': heat,
        'correlations': correlations,
        'cot': cot,
        'pulse': pulse,
        'macro': macro,
        'regime': regime,
        'quant_by_symbol': quant_by_symbol,
        'quant_anomalies': quant_anomalies,
        'docket': docket,
        'liquidity': liquidity,
        'breadth': breadth,
        'regime_clock': regime_clock,
        'rotation': rotation,
        'flows': flows,
        'desk': desk,
    }


@reports_bp.route('/report/morning')
@login_required
def morning_report():
    """Display the morning market analysis report.

    Free users: summary tab only. Member: all tabs + standard personal.
    Premium: all tabs + full personal + audio. No per-view charge (subscription-based).
    """
    from carrac.auth.decorators import get_request_tier
    tier = get_request_tier()

    # morning_report_opened funnel event. Fires every open (not
    # first-only) so we can track repeat engagement across the cohort.
    try:
        uid = session.get('user_id')
        if uid:
            from carrac.integrations import on_funnel_event
            on_funnel_event(uid, 'morning_report_opened', {'tier': tier})
    except Exception:
        pass

    total = sum(len(a['timeframes']) for a in MORNING_ASSETS if not a.get('ratio'))

    # Board (prices + consensio) is user-independent and precomputed by the
    # worker every ~30 min — read the warm cache; fall back to an inline build
    # only when cold (just after a worker restart, before the first warm cycle).
    from carrac.precompute import response_cache
    board = response_cache.get('morning_board')
    if board is None:
        board = build_morning_board()
    prices = board.get('prices') or {}
    consensio_data = board.get('consensio') or {}

    # Fetch alerts for display (use logged-in user's ID)
    # Filter to price-bearing alerts only — the template renders a "Target" price column
    # and will crash with a format error on alert types that carry no price
    # (trendline_break, regime_transition, derivatives_regime, indicator).
    alerts = []
    try:
        uid = session.get('user_id')
        raw_alerts = push_notifications.get_user_alerts(str(uid) if uid else 'admin', active_only=False)
        alerts = [a for a in raw_alerts if a.get('price') is not None]
    except Exception:
        pass

    is_admin = bool(session.get('is_admin'))
    editorial = _use_editorial(request, is_admin)
    template = "morning_report_editorial.html" if editorial else "morning_report.html"

    # NOTABLE_BANDS controls which rarity chips the spotlight cards surface;
    # passed to both templates (morning_report.html doesn't use it).
    from carrac.facts.translate import NOTABLE_BANDS as notable_bands

    # Editorial-only gather (clusters/board/heat + R1/R2 bands + side modules).
    # Pure refactor: _editorial_context() is the exact former inline block
    # here, extracted so /brief (Tier 2.1) can reuse it without duplication.
    # Non-editorial mode keeps the same safe defaults the inline block used
    # to leave in place when `if editorial:` was False.
    editorial_ctx = {
        'facts_list': [], 'cards': [], 'clusters': [], 'board': [], 'heat': None,
        'correlations': None, 'cot': None, 'pulse': None, 'macro': None,
        'regime': None, 'quant_by_symbol': {}, 'quant_anomalies': None,
        'docket': None, 'liquidity': None, 'breadth': None,
        'regime_clock': None, 'rotation': None, 'flows': None, 'desk': None,
    }
    if editorial:
        editorial_ctx = _editorial_context(is_admin=is_admin, tier=tier)

    return render_template(template,
        assets=MORNING_ASSETS,
        date=datetime.now().strftime('%Y-%m-%d'),
        total_charts=total,
        prices=prices,
        consensio=consensio_data,
        alerts=alerts,
        logged_in=bool(session.get('user_id')),
        is_admin=is_admin,
        tier=session.get('tier', 'admin' if is_admin else 'free'),
        notable_bands=notable_bands,
        **editorial_ctx,
    )


# ------------------------------------------------------------------
# Public Daily Brief (Tier 2.1) — the GENERAL report, no login required
# ------------------------------------------------------------------

def _gather_general_report_json() -> "dict | None":
    """Build the exact JSON dict ``/api/report/morning/latest?as=general``
    returns for an anonymous, non-operator, non-personalized caller.

    Reuses the SAME sanitization the authed API uses
    (``carrac.reports.sanitize.report_text_for_viewer``) — never
    re-implements it — plus the SAME delta-header primitives already defined
    in this module (``_get_cached_crypto_prices`` / ``_get_db``), which the
    authed handler itself imports from here. See
    ``carrac/fastapi_app/routers/reports.py::get_latest_morning_report`` for
    the full (tier-branching) version this mirrors: an anonymous caller can
    never be the operator and never gets the personal splice, so only that
    slice of the handler is reproduced.
    """
    from carrac.content_db import get_content_db
    from carrac.reports.sanitize import public_x_candidates, report_text_for_viewer

    db = get_content_db()
    report = db.get_latest_report(report_type='general')
    if not report:
        return None

    content = report['content'] or ''
    marker = '<!-- PERSONAL_SECTION_START -->'
    idx = content.find(marker)
    if idx != -1:
        content = content[:idx]
    # An anonymous caller is never the operator. The filter includes the
    # Deribit prose scrub: /brief serves the STORED latest, which is a
    # pre-gate report until the next 06:00 UTC run.
    result: dict = {
        'created_at': report.get('created_at', ''),
        'id': report.get('id'),
        **report_text_for_viewer({**report, 'content': content}, 'general', operator=False),
    }

    meta = None
    if report.get('metadata'):
        try:
            meta = json.loads(report['metadata'])
        except (json.JSONDecodeError, TypeError):
            meta = None

    if isinstance(meta, dict):
        candidates = meta.get('x_candidates')
        if isinstance(candidates, list):
            result['x_candidates'] = public_x_candidates(candidates)

    if meta and meta.get('report_prices'):
        try:
            report_prices = meta['report_prices']
            crypto_prices = _get_cached_crypto_prices() or {}
            database = _get_db()
            deltas = []
            for asset, rp in report_prices.items():
                if rp is None:
                    continue
                cp = _live_price_for_delta(asset, crypto_prices, database)
                if cp is not None and rp:
                    pct = (cp - rp) / rp * 100
                    if abs(pct) >= 0.3:
                        deltas.append({
                            'asset': asset,
                            'report_price': rp,
                            'current_price': cp,
                            'pct_change': round(pct, 2),
                        })
            deltas.sort(key=lambda d: abs(d['pct_change']), reverse=True)
            result['delta_header'] = {
                'report_time': meta.get('report_timestamp', report.get('created_at', '')),
                'deltas': deltas[:15],
            }
        except Exception:
            pass

    return result


def _gather_brief_inline() -> dict:
    """Server-side gather of the 4 JSON blobs the editorial page's JS
    normally fetches from authed endpoints (`/api/report/morning/latest
    ?as=general`, `/api/market-sentiment`, `/api/market/breadth`,
    `/api/quant/snapshot`), so an anonymous `/brief` visitor's browser never
    has to call them itself. Each blob is None on any failure (non-fatal) —
    the page's JS falls back to its own fetch, which degrades the same way
    it already does today for a logged-out visitor (a 401 → caught `.catch`).
    """
    try:
        report = _gather_general_report_json()
    except Exception:
        report = None

    sentiment = None
    try:
        from carrac.caches import get_cached_sentiment, get_stale_sentiment
        sentiment = get_cached_sentiment() or get_stale_sentiment()
    except Exception:
        sentiment = None

    breadth = None
    try:
        from carrac.market.breadth import get_breadth
        breadth = get_breadth(force_refresh=False)
    except Exception:
        breadth = None

    snapshot = None
    try:
        from quant.api.routes import build_snapshot
        snapshot = build_snapshot('BTC')
    except Exception:
        snapshot = None

    return {
        'report': report,
        'sentiment': sentiment,
        'breadth': breadth,
        'snapshot': snapshot,
    }


# The page cache outlives a deploy, so the key version must change whenever
# the rendered HTML changes in a way old entries must not survive: stale
# entries would keep serving for their full TTL. v3 entries embed
# window.__briefData rendered after the anomaly/cluster gate, so they no
# longer carry gated values such as SKEW_25D_ETH, GEX_ETH or OPTIONS_IV_ETH.
# v4 entries hold a nonce placeholder instead of the rendering request's CSP
# nonce, which a different request's response header would never match.
_BRIEF_CACHE_KEY = 'brief:html:v4'
_BRIEF_CACHE_TTL = 600  # 10 minutes


@reports_bp.route('/brief')
@limiter.limit("30 per minute")
def brief():
    """Public daily brief (Tier 2.1) — the operator-neutral GENERAL report,
    rendered on the full editorial page, no login required.

    Reuses ``_editorial_context()`` (the SAME gather ``morning_report()``
    uses in editorial mode) and the SAME as=general shaping/sanitization the
    authed ``/api/report/morning/latest`` API uses (see
    ``_gather_general_report_json`` above) — never a separate
    implementation. The 4 JSON blobs the page's JS normally fetches from
    authed endpoints are server-injected as ``window.__briefData`` (see
    ``_gather_brief_inline``), so an anonymous visitor's browser never needs
    a session or API key to see them.

    Page-cached in Redis for 10 minutes (key ``_BRIEF_CACHE_KEY``); any cache
    error is non-fatal — falls straight through to a live render (never a
    500 on a cache hiccup).
    """
    # With no report being produced, /brief must not present the
    # last stored report on the live editorial page as if it were today's.
    # Render the archive index with a paused notice instead (uncached — the
    # page is cheap and the flag must take effect on the first request).
    from morning_engine.config import reports_paused
    if reports_paused():
        from carrac.reports.brief_archive import list_recent_general_reports, _ARCHIVE_LIMIT
        return render_template(
            'brief_archive.html',
            reports=list_recent_general_reports(_ARCHIVE_LIMIT),
            paused=True,
        )

    r = None
    try:
        from carrac.redis_cache import get_redis
        r = get_redis()
        if r:
            cached = r.get(_BRIEF_CACHE_KEY)
            if cached:
                if isinstance(cached, bytes):
                    cached = cached.decode('utf-8', 'replace')
                return Response(
                    cached.replace(CSP_NONCE_PLACEHOLDER, getattr(g, 'csp_nonce', '')),
                    mimetype='text/html',
                )
    except Exception:
        r = None

    total = sum(len(a['timeframes']) for a in MORNING_ASSETS if not a.get('ratio'))

    from carrac.precompute import response_cache
    board = response_cache.get('morning_board')
    if board is None:
        board = build_morning_board()
    prices = board.get('prices') or {}
    consensio_data = board.get('consensio') or {}

    from carrac.facts.translate import NOTABLE_BANDS as notable_bands
    # /brief is a single shared, Redis-cached render for every anonymous
    # visitor (see _BRIEF_CACHE_KEY above) — is_admin/tier are hardcoded
    # anonymous-public values here (matching the template context below),
    # never a real caller's session, so the Derivatives Desk always renders
    # ABSENT-unless-flagged and, when present, always LOCKED on this route.
    editorial_ctx = _editorial_context(is_admin=False, tier='free')
    brief_inline = _gather_brief_inline()

    from carrac.reports.markdown_render import render_brief_html
    story_html = ''
    _report = brief_inline.get('report') if brief_inline else None
    if _report and _report.get('content'):
        story_html = render_brief_html(_report['content'])

    html = render_template(
        'morning_report_editorial.html',
        assets=MORNING_ASSETS,
        date=datetime.now().strftime('%Y-%m-%d'),
        total_charts=total,
        prices=prices,
        consensio=consensio_data,
        alerts=[],
        logged_in=False,
        is_admin=False,
        tier='free',
        notable_bands=notable_bands,
        public_brief=True,
        brief_inline=brief_inline,
        story_html=story_html,
        csp_nonce=CSP_NONCE_PLACEHOLDER,
        **editorial_ctx,
    )

    try:
        if r:
            r.setex(_BRIEF_CACHE_KEY, _BRIEF_CACHE_TTL, html)
    except Exception:
        pass

    return Response(
        html.replace(CSP_NONCE_PLACEHOLDER, getattr(g, 'csp_nonce', '')),
        mimetype='text/html',
    )


# ------------------------------------------------------------------
# Save Morning Report
# ------------------------------------------------------------------

@reports_bp.route('/api/report/morning', methods=['POST'])
@csrf.exempt
@limiter.limit("5 per minute;20 per hour")
def save_morning_report():
    """Save a morning report analysis. Accepts the engine key or an admin session.

    A non-admin session (any logged-in user, incl. free tier) is
    NOT sufficient. Only the internal engine key or an
    admin session may save.

    The four sibling save_* routes enforce the same rule. A test that sweeps
    routes is only as wide as the list it iterates, so this primary route must
    be named in that list explicitly.
    """
    if request.args.get('key'):
        logger.warning(
            "[SECURITY] Rejected: API key passed as URL parameter on %s. Use X-Internal-Key or X-API-Key header.",
            request.path,
        )
        return jsonify({'error': 'API key via URL parameter is deprecated. Use X-Internal-Key or X-API-Key header.'}), 400
    # Allow API key auth for automated scripts (the morning engine)
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key):
        if 'user_id' not in session:
            return jsonify({'error': 'unauthorized'}), 401
        if not session.get('is_admin'):
            return jsonify({'error': 'unauthorized'}), 403

    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'content is required'}), 400

    # Content size limit (100KB) to prevent abuse
    if len(data['content']) > 100_000:
        return jsonify({'error': 'content too large (max 100KB)'}), 413

    from carrac.content_db import get_content_db
    db = get_content_db()

    # Store report metadata (steering YAML, sentiment, etc.)
    metadata_raw = data.get('metadata')
    metadata_str = None
    if metadata_raw and isinstance(metadata_raw, dict):
        metadata_str = json.dumps(metadata_raw)
    elif metadata_raw and isinstance(metadata_raw, str):
        metadata_str = metadata_raw

    # Dedup operator pings to the FIRST morning report of the UTC day. The 5
    # regional dispatches each re-save the shared report (cache-cold), which
    # would otherwise fire 5x ntfy + 5x full-report Telegram for one report.
    # generation_date is the stable UTC-day key (created_at is bumped on upsert).
    from datetime import datetime as _dt, timezone as _tz
    _today = _dt.now(_tz.utc).strftime('%Y-%m-%d')
    _prev = db.get_latest_report(report_type='morning')
    _first_today = not (_prev and str(_prev.get('generation_date') or '')[:10] == _today)

    report_id = db.save_report(
        content=data['content'],
        assets_included=data.get('assets', []),
        generation_time_ms=data.get('generation_time_ms', 0),
        report_type='morning',
        metadata=metadata_str,
        source_briefing=_extract_source_briefing(data, 'morning'),
    )

    # Operator notification — ONCE per day (first dispatch only), short + linked.
    if _first_today:
        try:
            ntfy_topic = current_app.config.get('NTFY_MARKET_TOPIC')
            if ntfy_topic:
                import requests as req
                assets_list = data.get('assets', [])
                summary = f"Morning report ready — {len(assets_list)} assets"
                req.post(f'{_ntfy_base()}/{ntfy_topic}', data=summary.encode('utf-8'), headers={
                    'Title': 'Morning Market Report Ready',
                    'Tags': 'chart_with_upwards_trend',
                    'Click': f'{site_base_url()}/report/morning/archive',
                }, timeout=5)
        except Exception:
            pass  # Don't fail the save if notification fails

        # Telegram — short ping + link, NOT the full report dump.
        try:
            send_telegram_message(
                f'📊 Morning report ready — {site_base_url()}/report/morning'
            )
        except Exception:
            pass

    # Pre-render all morning report chart screenshots in background
    _prerender_morning_charts()

    return jsonify({'success': True, 'report_id': report_id})


# ------------------------------------------------------------------
# Pre-render helper (background thread)
# ------------------------------------------------------------------

# Serialized to 1 so the background pre-render can never occupy more than a
# single gunicorn worker at a time — 3 stay free for live user requests.
# (Was 2, which — combined with the loopback /api/screenshot calls — ate 2-3
# of 4 workers for ~65 min on every regional report save.)
_PRERENDER_MAX_WORKERS = 1


def _prerender_morning_charts():
    """Spawn background thread to pre-render all morning report chart PNGs into disk cache."""
    screenshot_key = current_app.config.get('SCREENSHOT_API_KEY', '')

    chart_jobs = []
    prerender_assets = [
        {'symbol': 'SPX', 'timeframes': ['1D', '3D', '1W', '1M']},
        {'symbol': 'QQQ', 'timeframes': ['1D', '3D', '1W', '1M']},
        {'symbol': 'BTC', 'timeframes': ['4H', '1D', '3D', '1W', '1M']},
        {'symbol': 'GOLD', 'timeframes': ['1D', '3D', '1W', '1M']},
        {'symbol': 'SILVER', 'timeframes': ['1D', '3D', '1W', '1M']},
        {'symbol': 'OIL', 'timeframes': ['1D', '3D', '1W', '1M']},
        {'symbol': 'ETH', 'timeframes': ['4H', '1D', '3D', '1W', '1M']},
        {'symbol': 'XRP', 'timeframes': ['4H', '1D', '3D', '1W', '1M']},
        {'symbol': 'TOTAL', 'timeframes': ['1D', '3D', '1W', '1M']},
        {'symbol': 'DXY', 'timeframes': ['1D', '3D', '1W', '1M']},
    ]
    for a in prerender_assets:
        for tf in a['timeframes']:
            chart_jobs.append((a['symbol'], tf))

    def render_one(job):
        sym, tf = job
        try:
            import requests as req
            base = f'http://localhost:5000/api/screenshot/{sym}/{tf}'
            headers = {'X-Internal-Key': screenshot_key}
            # Pre-render thumbnail (640x360 Q40) + full size (1280x720 Q75)
            # No indicators param — uses saved user preferences automatically
            req.get(f'{base}?width=640&height=360&quality=40', headers=headers, timeout=45)
            req.get(f'{base}?width=1280&height=720&quality=75', headers=headers, timeout=45)
            logger.info(f"Pre-rendered {sym}/{tf} (2 sizes)")
        except Exception as e:
            logger.warning(f"Pre-render failed {sym}/{tf}: {e}")

    def run_prerender():
        # Cross-worker overlap guard: regional report saves (asia/europe/us…)
        # each trigger this, and a run can take many minutes. Without a shared
        # lock, two dispatches stack screenshot storms on the same worker pool.
        # NX lock in Redis (db0) with a 90-min safety TTL; best-effort — if
        # Redis is unavailable we proceed unguarded rather than skip rendering.
        r = None
        try:
            from carrac.redis_cache import get_redis
            r = get_redis()
            if not r.set('mcd:prerender:running', '1', nx=True, ex=5400):
                logger.info("Pre-render already running elsewhere — skipping this dispatch")
                return
        except Exception:
            r = None
        try:
            logger.info(f"Pre-rendering {len(chart_jobs)} morning report charts...")
            with ThreadPoolExecutor(max_workers=_PRERENDER_MAX_WORKERS) as pool:
                pool.map(render_one, chart_jobs)
            logger.info("Morning report chart pre-render complete")
        except Exception:
            # Without this, exceptions in the pre-render wrapper thread are
            # silently lost. logger.exception makes them a first-class event.
            logger.exception("Morning report chart pre-render failed")
        finally:
            if r is not None:
                try:
                    r.delete('mcd:prerender:running')
                except Exception:
                    pass

    t = threading.Thread(target=run_prerender, daemon=True)
    t.start()


# ------------------------------------------------------------------
# Update Report Summary
# ------------------------------------------------------------------

@reports_bp.route('/api/report/morning/<int:report_id>/summary', methods=['PUT'])
@csrf.exempt
def save_report_summary(report_id):
    """Save an executive summary for a morning report. Engine-only — API key required.

    session-auth fallback removed. The shared morning `reports` table
    is not scoped per user, so any logged-in user could overwrite the summary
    seen by everyone. This endpoint is only invoked by the morning engine via
    the shared admin key.
    """
    api_key = (
        request.headers.get('X-Internal-Key')  # server-to-server (morning_engine)
        or request.headers.get('X-API-Key')    # external SDK callers
    )
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    data = request.get_json()
    if not data or not data.get('summary'):
        return jsonify({'error': 'summary is required'}), 400

    from carrac.db_pool import adapt_query, get_reports_conn
    try:
        with get_reports_conn() as conn:
            conn.execute(adapt_query('UPDATE reports SET summary = ? WHERE id = ?'), (data['summary'], report_id))
    except Exception as e:
        logger.error(f"Error saving report summary: {e}")
        return jsonify({'error': 'Failed to save summary'}), 500
    return jsonify({'success': True, 'report_id': report_id})


# ------------------------------------------------------------------
# Update Report Metadata
# ------------------------------------------------------------------

@reports_bp.route('/api/report/morning/<int:report_id>/metadata', methods=['PUT'])
@csrf.exempt
def save_report_metadata(report_id):
    """Save report internals metadata (steering YAML, sentiment, etc.).

    engine-only endpoint — session fallback removed (see save_report_summary).
    """
    api_key = (
        request.headers.get('X-Internal-Key')  # server-to-server (morning_engine)
        or request.headers.get('X-API-Key')    # external SDK callers
    )
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'error': 'metadata is required'}), 400

    metadata_str = json.dumps(data)
    # Limit metadata size to 500KB
    if len(metadata_str) > 500_000:
        return jsonify({'error': 'metadata too large (max 500KB)'}), 413

    from carrac.db_pool import adapt_query, get_reports_conn
    try:
        with get_reports_conn() as conn:
            conn.execute(adapt_query('UPDATE reports SET metadata = ? WHERE id = ?'), (metadata_str, report_id))
    except Exception as e:
        logger.error(f"Error saving report metadata: {e}")
        return jsonify({'error': 'Failed to save metadata'}), 500
    return jsonify({'success': True, 'report_id': report_id})


# ------------------------------------------------------------------
# Save Report Audio Script (engine → dedicated column)
# ------------------------------------------------------------------

@reports_bp.route('/api/report/morning/<int:report_id>/audio-script', methods=['PUT'])
@csrf.exempt
def save_report_audio_script(report_id):
    """Save the public audio briefing script for a report. API key auth only.

    engine-only endpoint — session fallback removed (see save_report_summary).
    """
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    data = request.get_json()
    if not data or not data.get('audio_script'):
        return jsonify({'error': 'audio_script is required'}), 400

    if len(data['audio_script']) > 50_000:
        return jsonify({'error': 'audio_script too large (max 50KB)'}), 413

    from carrac.content_db import get_content_db
    cdb = get_content_db()
    success = cdb.update_report_audio_script(report_id, data['audio_script'])
    if not success:
        return jsonify({'error': 'Failed to save audio_script'}), 500
    return jsonify({'success': True, 'report_id': report_id})


# ------------------------------------------------------------------
# Save Personal Report Section (engine → per-user content)
# ------------------------------------------------------------------

@reports_bp.route('/api/report/<int:report_id>/personal', methods=['POST'])
@csrf.exempt
def save_personal_section(report_id):
    """Save a per-user personal report section. API key auth only (engine use).

    No source_briefing accepted or persisted here, on purpose.
    The per-user briefing (trader profile, positions, plays/journal text) is
    materially more sensitive than the shared market briefing persisted for
    the other report types and is already treated as untrusted content
    (carrac/grounding/escape.py) — see dashboard_api.py::save_personal_section
    for the full reasoning.
    """
    api_key = (
        request.headers.get('X-Internal-Key')  # server-to-server (morning_engine)
        or request.headers.get('X-API-Key')    # external SDK callers
    )
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    data = request.get_json()
    if not data or not data.get('content') or not data.get('user_id'):
        return jsonify({'error': 'content and user_id are required'}), 400

    if len(data['content']) > 100_000:
        return jsonify({'error': 'content too large (max 100KB)'}), 413

    from carrac.content_db import get_content_db
    cdb = get_content_db()
    section_id = cdb.save_personal_section(
        report_id=report_id,
        user_id=int(data['user_id']),
        content=data['content'],
        cost_cents=data.get('cost_cents', 0),
        input_tokens=data.get('input_tokens', 0),
        output_tokens=data.get('output_tokens', 0),
        audio_script=data.get('audio_script'),
    )
    return jsonify({'success': True, 'section_id': section_id})


# ------------------------------------------------------------------
# Report Audio (TTS voice briefing)
# ------------------------------------------------------------------

def _audio_dir():
    """Return path to audio storage directory, creating it if needed."""
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'data', 'audio')
    os.makedirs(base, exist_ok=True)
    return base


@reports_bp.route('/api/report/<int:report_id>/audio', methods=['POST'])
@csrf.exempt
def save_report_audio(report_id):
    """Save TTS audio for a report. Engine use only (API key auth).

    Expects raw MP3 bytes in the request body with Content-Type: audio/mpeg.
    """
    api_key = (
        request.headers.get('X-Internal-Key')  # server-to-server (morning_engine)
        or request.headers.get('X-API-Key')    # external SDK callers
    )
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    audio_data = request.get_data()
    if not audio_data or len(audio_data) < 100:
        return jsonify({'error': 'no audio data'}), 400

    # Limit to 10MB
    if len(audio_data) > 10_000_000:
        return jsonify({'error': 'audio too large (max 10MB)'}), 413

    # Validate MP3 magic bytes: ID3 tag (0x49 0x44 0x33) or MPEG sync (0xFF 0xFB/F3/F2/E3/E2)
    if not (audio_data[:3] == b'ID3' or
            (audio_data[0] == 0xFF and audio_data[1] in (0xFB, 0xF3, 0xF2, 0xE3, 0xE2))):
        return jsonify({'error': 'invalid audio format (MP3 only)'}), 400

    filepath = os.path.join(_audio_dir(), f'report_{report_id}.mp3')
    with open(filepath, 'wb') as f:
        f.write(audio_data)

    logger.info(f"Saved TTS audio for report #{report_id}: {len(audio_data):,} bytes")
    return jsonify({'success': True, 'size_bytes': len(audio_data)})


@reports_bp.route('/api/report/<int:report_id>/audio', methods=['GET'])
@auth_or_key_required
def get_report_audio(report_id):
    """Serve a report's narration as MP3. Operator only: it is spoken from the
    report's full text (see ``report_text_for_viewer``)."""
    if not _viewer_is_operator():
        return jsonify({'error': 'no audio for this report'}), 404
    filepath = os.path.join(_audio_dir(), f'report_{report_id}.mp3')
    if not os.path.exists(filepath):
        return jsonify({'error': 'no audio for this report'}), 404

    return send_file(filepath, mimetype='audio/mpeg')


@reports_bp.route('/api/report/<int:report_id>/personal/<int:user_id>/audio', methods=['POST'])
@csrf.exempt
def save_personal_audio(report_id, user_id):
    """Save personal TTS audio for a user's report section. API key auth only."""
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    audio_data = request.get_data()
    if not audio_data or len(audio_data) < 100:
        return jsonify({'error': 'no audio data'}), 400
    if len(audio_data) > 10_000_000:
        return jsonify({'error': 'audio too large (max 10MB)'}), 413

    # Same MP3 magic-byte check as save_report_audio: ID3 tag or MPEG sync.
    # Without this, any binary blob (executable, ZIP, etc.) could be written
    # as an .mp3 on disk and later served back with audio/mpeg.
    if not (audio_data[:3] == b'ID3' or
            (audio_data[0] == 0xFF and audio_data[1] in (0xFB, 0xF3, 0xF2, 0xE3, 0xE2))):
        return jsonify({'error': 'invalid audio format (MP3 only)'}), 400

    filepath = os.path.join(_audio_dir(), f'report_{report_id}_personal_{user_id}.mp3')
    with open(filepath, 'wb') as f:
        f.write(audio_data)

    logger.info(f"Saved personal audio for report #{report_id} user #{user_id}: {len(audio_data):,} bytes")
    return jsonify({'success': True, 'size_bytes': len(audio_data)})


@reports_bp.route('/api/report/<int:report_id>/personal/<int:user_id>/audio', methods=['GET'])
@auth_or_key_required
def get_personal_audio(report_id, user_id):
    """Serve personal TTS audio. User can only access their own audio."""
    # Users can only access their own personal audio
    req_user = session.get('user_id')
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key) and (not req_user or int(req_user) != user_id):
        return jsonify({'error': 'unauthorized'}), 403

    filepath = os.path.join(_audio_dir(), f'report_{report_id}_personal_{user_id}.mp3')
    if not os.path.exists(filepath):
        return jsonify({'error': 'no personal audio'}), 404

    return send_file(filepath, mimetype='audio/mpeg')


# How old the newest 'morning' row may be before its narration
# stops counting as "the latest report's audio". The morning report is a daily
# artifact, so anything past two days is a stopped pipeline, not a late run —
# and scripts/report-health-check.sh already owns the "a daily report is
# missing" alarm at 30h, so this bound stays silent and simply declines to
# serve rather than adding a second voice to an alarm that already fires.
_LATEST_AUDIO_MAX_AGE_DAYS = 2.0


@reports_bp.route('/api/report/morning/latest/audio', methods=['GET'])
@auth_or_key_required
def get_latest_report_audio():
    """The latest morning narration: the caller's personal audio, else the
    shared one for the operator only.

    404 unless the morning report is enabled and fresh, so stale audio never
    plays beside current text.
    """
    import os as _os
    from carrac.content_db import get_content_db
    db = get_content_db()

    _morning_enabled = _os.environ.get(
        'ENABLE_MORNING_REPORT', 'true').lower() in ('true', '1', 'yes')
    if not _morning_enabled:
        return jsonify({'error': 'no current morning report'}), 404

    report = db.get_latest_report(report_type='morning',
                                  max_age_days=_LATEST_AUDIO_MAX_AGE_DAYS)
    if not report:
        return jsonify({'error': 'no current morning report'}), 404

    report_id = report['id']

    # Prefer personal audio for logged-in users
    user_id = session.get('user_id')
    if user_id:
        personal_path = os.path.join(_audio_dir(), f"report_{report_id}_personal_{user_id}.mp3")
        if os.path.exists(personal_path):
            return send_file(personal_path, mimetype='audio/mpeg')

    # The shared narration is spoken from the operator's full text.
    if not _viewer_is_operator():
        return jsonify({'error': 'no audio for latest report'}), 404
    filepath = os.path.join(_audio_dir(), f"report_{report_id}.mp3")
    if not os.path.exists(filepath):
        return jsonify({'error': 'no audio for latest report'}), 404

    return send_file(filepath, mimetype='audio/mpeg')


# ------------------------------------------------------------------
# Morning Report Archive
# ------------------------------------------------------------------

@reports_bp.route('/report/morning/archive')
@login_required
def morning_report_archive():
    """Display list of past morning reports."""
    from carrac.content_db import get_content_db
    content_db = get_content_db()
    reports = content_db.get_all_reports(report_type='morning', limit=50)
    return render_template('morning_report_archive.html', reports=reports)


# ------------------------------------------------------------------
# View Specific Morning Report
# ------------------------------------------------------------------

def _report_age_days(report: dict) -> int:
    """Return the age of a report in whole days (UTC), defensive about format.

    ``created_at`` may be:
      - a ``datetime`` object (Postgres via psycopg2 row_factory)
      - a string like "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD" (SQLite)
    Returns 0 if parsing fails so callers never raise.
    """
    from datetime import datetime, timezone, date as _date
    raw = report.get('created_at')
    if raw is None:
        return 0
    if isinstance(raw, datetime):
        ts = raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw
        return (datetime.now(timezone.utc) - ts).days
    if isinstance(raw, _date):
        # date (not datetime) — treat as midnight UTC
        ts = datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).days
    # String: try ISO datetime first, then date-only
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            ts = datetime.strptime(str(raw)[:19], fmt).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).days
        except ValueError:
            continue
    return 0


@reports_bp.route('/report/morning/<int:report_id>')
@login_required
def morning_report_view(report_id):
    """Display a specific past morning report."""
    from carrac.content_db import get_content_db
    content_db = get_content_db()
    report = content_db.get_report_by_id(report_id)
    if not report or report.get('report_type') != 'morning':
        return 'Report not found', 404
    report = {**report, **_text_for_viewer(report, 'morning')}
    return render_template('morning_report_view.html', report=report,
                           report_type='morning',
                           age_days=_report_age_days(report))


# ------------------------------------------------------------------
# Recent Morning Reports (for weekly digest)
# ------------------------------------------------------------------

_RECENT_REPORTS_TYPES = ('morning', 'general', 'afternoon', 'weekly', 'week_ahead')


@reports_bp.route('/api/report/morning/recent', methods=['GET'])
@auth_or_key_required
def get_recent_morning_reports():
    """Get recent reports of a given type as JSON.

    Used by the weekly digest script (defaults to 'morning'
    for the existing callers — the operator report page and the run_dispatch/
    run_morning narrative-continuity fetches — but run_weekly now passes
    type=general explicitly. 'morning' stops generating when
    ENABLE_MORNING_REPORT is false; a caller still defaulting to 'morning'
    would keep reading the same handful of legacy rows forever, which is the
    exact failure this endpoint's `type` param lets a caller opt out of.

    The engine reads with the internal key and gets the full text; every other
    caller gets ``report_text_for_viewer``'s cut.
    """
    limit = request.args.get('limit', 5, type=int)
    report_type = request.args.get('type', 'morning')
    if report_type not in _RECENT_REPORTS_TYPES:
        return jsonify({'error': f'Unknown report type: {report_type}'}), 400
    from carrac.content_db import get_content_db
    content_db = get_content_db()
    reports = content_db.get_all_reports(report_type=report_type, limit=limit)
    result = []
    for r in reports:
        full = content_db.get_report_by_id(r['id'])
        if full:
            result.append({
                'id': full['id'],
                'created_at': full.get('created_at', ''),
                **_text_for_viewer(full, report_type),
            })
    return jsonify(result)


# ------------------------------------------------------------------
# Engine Internal Endpoints (API key only)
# ------------------------------------------------------------------

@reports_bp.route('/api/engine/eligible-users', methods=['GET'])
@csrf.exempt
def get_eligible_users():
    """Get users eligible for personal report sections. Engine use only.

    Optional query params:
        region — filter by report_schedule region (europe, us_east, us_west, asia_pacific, asia).
                 Users with no report_schedule default to 'europe'.
        mode   — 'morning' (default) or 'week_ahead'. For week_ahead, also
                 requires the user to have opted in via prefs.weekly_ahead_enabled.
    """
    api_key = (
        request.headers.get('X-Internal-Key')  # server-to-server (morning_engine)
        or request.headers.get('X-API-Key')    # external SDK callers
    )
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    region_filter = request.args.get('region')
    mode = (request.args.get('mode') or 'morning').lower()

    users = []
    try:
        all_users = user_db.list_users()
        for u in all_users:
            if not u.get('is_active', True):
                continue
            uid = u['id']
            # Determine effective tier (is_admin flag overrides tier column)
            if u.get('is_admin'):
                tier = 'admin'
            else:
                tier = u.get('tier', 'free')
            # Only paid tiers get personal sections
            if tier not in ('member', 'tester', 'premium', 'admin'):
                continue

            # Get user's delivery region (default: europe for backward compat)
            prefs = user_db.get_preferences(uid)
            user_region = prefs.get('report_schedule', {}).get('region', 'europe')

            if region_filter and user_region != region_filter:
                continue

            # Week-ahead opt-in. Default off for existing users;
            # admins/testers always included so they can preview without
            # toggling. Stored inside morning_report_prefs to match the
            # existing camelCase convention (weeklyDigest, voiceBriefing, ...).
            if mode == 'week_ahead':
                mr_prefs = prefs.get('morning_report_prefs', {}) or {}
                opted_in = mr_prefs.get('weeklyAhead', False)
                if not opted_in and tier not in ('admin', 'tester'):
                    continue

            users.append({'id': uid, 'tier': tier, 'email': u.get('email', ''),
                          'region': user_region})
    except Exception as e:
        logger.error(f"Failed to get eligible users: {e}")

    return jsonify({'users': users})


@reports_bp.route('/api/engine/user-sources/<int:user_id>', methods=['GET'])
@csrf.exempt
def get_user_sources(user_id):
    """Get a user's analyst sources + trader profile context. Engine use only."""
    api_key = (
        request.headers.get('X-Internal-Key')  # server-to-server (morning_engine)
        or request.headers.get('X-API-Key')    # external SDK callers
    )
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    prefs = user_db.get_preferences(user_id)

    # Build formatted trader profile context
    trader_profile_text = ''
    try:
        from carrac.ai.context_builder import build_trader_profile_context
        trader_profile_text = build_trader_profile_context(user_id)
    except Exception:
        pass

    return jsonify({
        'x_handles': prefs.get('followed_x_handles', []),
        'yt_channels': prefs.get('followed_yt_channels', {}),
        'trader_profile': trader_profile_text,
        'morning_report_prefs': prefs.get('morning_report_prefs', {}),
        'report_indicators': prefs.get('report_indicators', []),
        'report_assets': prefs.get('report_assets', []),
    })


def _signal_diff_registry_symbols(symbols):
    """Keep only registry chart-asset symbols.

    Both ``get_morning_context``'s signal_diff and ``/api/report/diff`` build
    their "Signal Changes" (consensio flips / RSI 30-70 crossings / SMA
    crossovers) by iterating ``db.load_previous_snapshot()``, which spans the
    FULL tracked universe (``db.get_all_symbols()``, see
    carrac/precompute.py::_compute_assets_response) — not the 20-symbol
    chart registry. Left unfiltered, watchlist-only symbols (TSLA,
    META — none of them registry members) rode RSI-crossing rows
    straight into the shared afternoon/general "Signal Changes" section,
    same defect class as the RS Rankings leak and its Rotation-strip
    sibling.

    Same rule, same helper, as morning_engine/data_assembly.py's
    ``_registry_rs_only`` and carrac/facts/bands.py's ``build_rotation``:
    pass ``symbols`` through untouched when the registry is unreadable — an
    over-inclusive diff someone notices beats a vanished one nobody can
    explain.
    """
    from carrac.assets.registry import public_symbol_allowlist
    allowed = public_symbol_allowlist()
    if allowed is None:
        return list(symbols)
    return [s for s in symbols if str(s).upper() in allowed]


def _daily_rsi_pair(close):
    """(previous, current) DAILY Wilder RSI from one series, or (None, None).

    Comparing a hand-rolled SMA-based RSI against the snapshot's stored
    ``rsi`` is wrong three separate ways, which is why both ends come from
    one series here:

    1. **Different algorithm.** ``rolling(14).mean()`` is a simple moving
       average; the canonical RSI is Wilder's recursive smoothing
       (``consensio/indicators/rsi.py``, whose docstring notes it matches
       TradingView exactly). SMA-RSI is a different indicator, not a
       rounding difference.
    2. **Different timeframe.** The snapshot's ``rsi`` is the WEEKLY value —
       ``database.py::save_daily_snapshot`` stores ``asset['weekly_rsi']``, computed on
       ``tf_data['1W']`` at ``carrac/precompute.py``. So "previous" was a
       weekly reading and "current" a daily one.
    3. **Different from what the report prints.** The charts and the facts
       block render the canonical daily RSI, so the crossing was announced
       off a number appearing nowhere on the page.

    Example on a real NATGAS series: Wilder daily = 36.2 (what the report
    displays) while a hand-rolled SMA gives 29.3, which would cross below 30
    and publish "Nat Gas RSI entered oversold territory" on a report whose
    own table says 36.2.

    Taking BOTH ends from the same freshly-computed daily series is what
    makes the comparison meaningful — it also drops the cross-source
    dependency entirely, so a missing or stale snapshot can no longer
    manufacture a crossing. This mirrors the SMA-cross check a few lines
    below, which has always compared ``iloc[-1]`` against ``iloc[-2]`` of
    one series.
    """
    if close is None or len(close) < 16:
        return None, None
    try:
        from consensio.indicators.rsi import RSICalculator
        import pandas as _pd
        series = RSICalculator(14).calculate_series(_pd.DataFrame({'close': close}))
        if len(series) < 2:
            return None, None
        prev_v, cur_v = series.iloc[-2], series.iloc[-1]
        if _pd.isna(prev_v) or _pd.isna(cur_v):
            return None, None
        return round(float(prev_v), 1), round(float(cur_v), 1)
    except Exception as e:  # noqa: BLE001
        # Loud enough to find, non-fatal: a broken RSI must drop the crossing
        # rows, never take the whole signal-diff section down with it.
        logger.warning(f'daily RSI pair unavailable: {e}')
        return None, None


def _tag_signal_diff_asset_class(changes, db):
    """Attach ``is_crypto`` to each signal-diff change dict, in place.

    The registry filter above stops non-registry symbols
    (TSLA, META) from riding a "Signal Changes" table into the shared
    reports, but registry members span multiple asset classes too (SPX,
    GOLD, QQQ are registry AND equities/commodities). Without an explicit
    class tag, a report narrating "N alts are RSI-oversold" from this list
    has nothing stopping it from folding a registry equity into a crypto
    cluster. Classification via database.py::OHLCVDatabase._is_crypto — the
    same helper that separates crypto from equity/commodity/FX symbols. Returns ``changes`` for convenience.
    """
    for c in changes:
        c['is_crypto'] = db._is_crypto(c.get('symbol', ''))
    return changes


@reports_bp.route('/api/engine/morning-context', methods=['GET'])
@csrf.exempt
def get_morning_context():
    """Preassemble all context data needed by the morning engine in one call.

    Returns every data source the engine needs, with per-source availability
    flags and graceful fallbacks. One call replaces 9 separate HTTP calls,
    which would overwhelm gunicorn workers.

    GET /api/engine/morning-context?key=...
    """
    api_key = (
        request.headers.get('X-Internal-Key')  # server-to-server (morning_engine)
        or request.headers.get('X-API-Key')    # external SDK callers
    )
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    import time as _time
    from datetime import timedelta
    from carrac.content_db import get_content_db

    t0 = _time.time()
    content_db = get_content_db()
    db = _get_db()
    consensio = _get_consensio()

    results = {}
    availability = {}

    # --- Helper: run with retry + fallback ---
    def _safe(name, fn, fallback=None, retries=2):
        for attempt in range(retries + 1):
            try:
                val = fn()
                availability[name] = 'ok'
                return val
            except Exception as e:
                if attempt < retries:
                    _time.sleep(0.5 * (attempt + 1))
                else:
                    logger.warning(f"morning-context: {name} failed after {retries + 1} attempts: {e}")
                    availability[name] = f'error: {e}'
                    return fallback

    # 1. Alerts
    def _fetch_alerts():
        alerts = push_notifications.get_user_alerts('admin', active_only=False)
        return {'alerts': alerts}
    results['alerts'] = _safe('alerts', _fetch_alerts, {'alerts': []})

    # 2. Positions
    def _fetch_positions():
        import json as json_mod
        positions = []
        for dtype in ('position', 'long_position', 'short_position'):
            # -1 sentinel = legitimate cross-user read for admin morning report.
            drawings = content_db.drawing_list(drawing_type=dtype, user_id=-1)
            for d in drawings:
                meta = d.get('metadata') or {}
                if isinstance(meta, str):
                    try:
                        meta = json_mod.loads(meta)
                    except Exception:
                        meta = {}
                if meta.get('archived'):
                    continue
                points = meta.get('points', [])
                if len(points) < 3:
                    continue
                entry_v = points[0].get('value', 0)
                sl_v = points[1].get('value', 0)
                tp_v = points[2].get('value', 0)
                direction = 'long' if sl_v < entry_v else 'short'
                risk = abs(entry_v - sl_v)
                reward = abs(tp_v - entry_v)
                rr = round(reward / risk, 2) if risk > 0 else 0
                positions.append({
                    'id': d.get('id'),
                    'asset': d.get('asset', ''),
                    'timeframe': d.get('timeframe', ''),
                    'direction': direction,
                    'entry': round(entry_v, 2),
                    'stop_loss': round(sl_v, 2),
                    'take_profit': round(tp_v, 2),
                    'risk_reward': rr,
                    'label': d.get('label', ''),
                    'created_at': d.get('created_at', ''),
                })
        return {'positions': positions, 'count': len(positions)}
    results['positions'] = _safe('positions', _fetch_positions, {'positions': [], 'count': 0})

    # 3. Drawings context (enriched) — call via test client (in-process, no worker contention)
    def _fetch_scenarios():
        client = current_app.test_client()
        resp = client.get(
            '/api/drawings/context?assets=all&enrich=1',
            headers={'X-Internal-Key': api_key},
        )
        data = resp.get_json()
        return data.get('context', '') if data else 'Scenarios unavailable.'
    results['scenarios'] = _safe('scenarios', _fetch_scenarios, 'Scenarios unavailable.')

    # 4. Signal diff
    def _fetch_signal_diff():
        prev_snapshot = db.load_previous_snapshot()
        if not prev_snapshot:
            return {'changes': [], 'high_count': 0, 'medium_count': 0}
        changes = []
        symbols = [k for k in prev_snapshot.keys() if not k.startswith('_')]
        symbols = _signal_diff_registry_symbols(symbols)
        for symbol in symbols:
            prev = prev_snapshot[symbol]
            prev_1d = prev.get('signal_1d', 0)
            prev_3d = prev.get('signal_3d', 0)
            prev_1w = prev.get('signal_1w', 0)
            prev_rsi = prev.get('rsi')
            try:
                data = db.get_multi_timeframe_data(symbol)
                if not data or data.get('1D') is None or data['1D'].empty:
                    continue
                result = consensio.calculate_multi_timeframe(data['1D'], data['3D'], data['1W'])
                cur_1d = result['1D']['signal']
                cur_3d = result['3D']['signal']
                cur_1w = result['1W']['signal']
                for tf, cur, prev_val in [('1D', cur_1d, prev_1d), ('3D', cur_3d, prev_3d), ('1W', cur_1w, prev_1w)]:
                    if cur != prev_val:
                        direction = 'bullish' if cur > prev_val else 'bearish'
                        sig = 'high' if tf == '1D' else 'medium'
                        changes.append({
                            'symbol': symbol, 'field': f'consensio_{tf}',
                            'old_value': prev_val, 'new_value': cur,
                            'change_type': f'{tf} signal flip ({direction})',
                            'significance': sig,
                        })
                close = data['1D']['close']
                if len(close) >= 15:
                    # Canonical daily Wilder RSI, both ends from
                    # this one series. Shadows the weekly snapshot value read
                    # above, deliberately — see _daily_rsi_pair.
                    prev_rsi, cur_rsi = _daily_rsi_pair(close)
                    if prev_rsi is not None and cur_rsi is not None:
                        if cur_rsi < 30 and prev_rsi >= 30:
                            changes.append({'symbol': symbol, 'field': 'rsi_14',
                                            'old_value': prev_rsi, 'new_value': cur_rsi,
                                            'change_type': 'RSI entered oversold (<30)', 'significance': 'high'})
                        elif cur_rsi > 70 and prev_rsi <= 70:
                            changes.append({'symbol': symbol, 'field': 'rsi_14',
                                            'old_value': prev_rsi, 'new_value': cur_rsi,
                                            'change_type': 'RSI entered overbought (>70)', 'significance': 'high'})
                        elif cur_rsi >= 30 and prev_rsi < 30:
                            changes.append({'symbol': symbol, 'field': 'rsi_14',
                                            'old_value': prev_rsi, 'new_value': cur_rsi,
                                            'change_type': 'RSI exited oversold (crossed 30)', 'significance': 'medium'})
                        elif cur_rsi <= 70 and prev_rsi > 70:
                            changes.append({'symbol': symbol, 'field': 'rsi_14',
                                            'old_value': prev_rsi, 'new_value': cur_rsi,
                                            'change_type': 'RSI exited overbought (crossed 70)', 'significance': 'medium'})
                if len(close) >= 200:
                    sma50 = close.rolling(50).mean()
                    sma200 = close.rolling(200).mean()
                    cur_cross = sma50.iloc[-1] > sma200.iloc[-1]
                    prev_cross = sma50.iloc[-2] > sma200.iloc[-2] if len(sma50) > 1 else cur_cross
                    if cur_cross != prev_cross:
                        cross_type = 'Golden Cross (SMA50 > SMA200)' if cur_cross else 'Death Cross (SMA50 < SMA200)'
                        changes.append({'symbol': symbol, 'field': 'sma_crossover',
                                        'old_value': 'golden' if prev_cross else 'death',
                                        'new_value': 'golden' if cur_cross else 'death',
                                        'change_type': cross_type, 'significance': 'high'})
            except Exception as e:
                logger.debug(f"Diff calc failed for {symbol}: {e}")
                continue
        changes = _tag_signal_diff_asset_class(changes, db)
        high_count = sum(1 for c in changes if c['significance'] == 'high')
        medium_count = sum(1 for c in changes if c['significance'] == 'medium')
        return {'changes': changes, 'high_count': high_count, 'medium_count': medium_count}
    results['signal_diff'] = _safe('signal_diff', _fetch_signal_diff,
                                   {'changes': [], 'high_count': 0, 'medium_count': 0})

    # 5. Relative strength
    def _fetch_rs():
        from carrac.charts.routes import _get_cached_rs, _compute_rs_rankings, _set_cached_rs
        cached = _get_cached_rs()
        if cached is not None:
            return {'assets': cached}
        rankings = _compute_rs_rankings()
        _set_cached_rs(rankings)
        return {'assets': rankings}
    results['rs_rankings'] = _safe('rs_rankings', _fetch_rs, {'assets': []})

    # 6. Market sentiment — call via test client (in-process, no worker contention)
    def _fetch_sentiment():
        client = current_app.test_client()
        resp = client.get(
            '/api/market-sentiment',
            headers={'X-Internal-Key': api_key},
        )
        return resp.get_json()
    results['sentiment'] = _safe('sentiment', _fetch_sentiment, {'status': 'error'})

    # 7. Sentiment history (24h)
    def _fetch_sentiment_history():
        from carrac.db_pool import get_conn, adapt_query
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        with get_conn() as conn:
            cur = conn.execute(
                adapt_query('SELECT * FROM sentiment_history WHERE timestamp > ? ORDER BY timestamp ASC'),
                (cutoff,),
            )
            rows = cur.fetchall()
        return {'count': len(rows), 'snapshots': [dict(r) for r in rows]}
    results['sentiment_history'] = _safe('sentiment_history', _fetch_sentiment_history,
                                         {'count': 0, 'snapshots': []})

    # 8. History context (14 days)
    def _fetch_history_context():
        ctx = content_db.build_history_context(days=14)
        return ctx
    results['history_context'] = _safe('history_context', _fetch_history_context, {})

    # 9. Yesterday's report
    def _fetch_yesterday():
        # An unbounded get_latest_report(report_type='morning') would serve a
        # stale document forever once 'morning' stops being written, and
        # run_dispatch and run_morning would render it verbatim under a
        # literal "## Yesterday" prompt header.
        #
        # resolve_prior_day_report() is the single implementation of that
        # choice — do not re-open the report_type question here.
        from carrac.reports.prior_day import resolve_prior_day_report
        report = resolve_prior_day_report(db=content_db)
        if not report:
            # Explicit absence. resolve_prior_day_report has already alarmed.
            return {'content': None}
        content = report['content'] or ''
        marker = '<!-- PERSONAL_SECTION_START -->'
        idx = content.find(marker)
        if idx != -1:
            content = content[:idx]
        return {
            'content': content,
            'created_at': report.get('created_at', ''),
            'id': report.get('id'),
            'summary': report.get('summary'),
            'report_type_used': report.get('report_type_used'),
        }
    results['yesterday_report'] = _safe('yesterday_report', _fetch_yesterday, {'content': None})

    elapsed = _time.time() - t0
    logger.info(f"morning-context preassembly: {elapsed:.1f}s — "
                f"{sum(1 for v in availability.values() if v == 'ok')}/{len(availability)} sources OK")

    return jsonify({
        'status': 'ok',
        'elapsed_ms': int(elapsed * 1000),
        'availability': availability,
        'data': results,
    })


# ------------------------------------------------------------------
# Save Weekly Report
# ------------------------------------------------------------------

@reports_bp.route('/api/report/weekly', methods=['POST'])
@csrf.exempt
@limiter.limit("5 per minute;20 per hour")
def save_weekly_report():
    """Save a weekly digest report. Accepts API key auth or an admin session.

    A non-admin session (any logged-in user, incl. free tier) is
    NOT sufficient. Only the internal engine key or an
    admin session may save.
    """
    if request.args.get('key'):
        logger.warning(
            "[SECURITY] Rejected: API key passed as URL parameter on %s. Use X-Internal-Key or X-API-Key header.",
            request.path,
        )
        return jsonify({'error': 'API key via URL parameter is deprecated. Use X-Internal-Key or X-API-Key header.'}), 400
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key):
        if 'user_id' not in session:
            return jsonify({'error': 'unauthorized'}), 401
        if not session.get('is_admin'):
            return jsonify({'error': 'unauthorized'}), 403

    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'content is required'}), 400

    from carrac.content_db import get_content_db
    content_db = get_content_db()
    report_id = content_db.save_report(
        content=data['content'],
        assets_included=data.get('assets', []),
        generation_time_ms=data.get('generation_time_ms', 0),
        report_type='weekly',
        source_briefing=_extract_source_briefing(data, 'weekly'),
    )

    # Send ntfy notification
    try:
        ntfy_topic = current_app.config.get('NTFY_MARKET_TOPIC')
        if ntfy_topic:
            import requests as req
            req.post(f'{_ntfy_base()}/{ntfy_topic}', data=f'Weekly digest #{report_id} ready'.encode('utf-8'), headers={
                'Title': 'Weekly Market Digest Ready',
                'Tags': 'calendar',
                'Click': f'{site_base_url()}/report/weekly/{report_id}',
            }, timeout=5)
    except Exception:
        pass

    # Send to Telegram — short ping + link, not the full digest dump.
    try:
        send_telegram_message(
            f'📅 Weekly market digest ready — {site_base_url()}/report/weekly/{report_id}'
        )
    except Exception:
        pass

    return jsonify({'success': True, 'report_id': report_id})


# ------------------------------------------------------------------
# Save Afternoon Delta Report
# ------------------------------------------------------------------

@reports_bp.route('/api/report/afternoon', methods=['POST'])
@csrf.exempt
@limiter.limit("5 per minute;20 per hour")
def save_afternoon_report():
    """Save an afternoon delta report. Accepts API key auth or an admin session.

    A non-admin session (any logged-in user, incl. free tier) is
    NOT sufficient. Only the internal engine key or an
    admin session may save.
    """
    if request.args.get('key'):
        logger.warning(
            "[SECURITY] Rejected: API key passed as URL parameter on %s. Use X-Internal-Key or X-API-Key header.",
            request.path,
        )
        return jsonify({'error': 'API key via URL parameter is deprecated. Use X-Internal-Key or X-API-Key header.'}), 400
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key):
        if 'user_id' not in session:
            return jsonify({'error': 'unauthorized'}), 401
        if not session.get('is_admin'):
            return jsonify({'error': 'unauthorized'}), 403

    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'content is required'}), 400

    from carrac.content_db import get_content_db
    content_db = get_content_db()

    # The afternoon delta is free-text "since this morning" prose
    # (Session Summary, Breadth note, Treasuries/Rates) that a
    # fact-check found gets these claims flatly wrong even though the real
    # numbers are available — either in the same document's own header
    # (spread) or in today's saved morning report (breadth). Check at the
    # one write chokepoint for every afternoon report, before it saves.
    # Non-fatal by design (carrac.reports.afternoon_grounding — same
    # asymmetry as carrac.compliance.deribit_gate.alert_if_barred_prose):
    # notify, never withhold a save.
    try:
        from datetime import datetime, timezone
        from carrac.reports.afternoon_grounding import check_afternoon_grounding
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        morning = content_db.get_report_by_date('general', today)
        check_afternoon_grounding(data['content'], morning.get('content') if morning else None)
    except Exception:
        # non-fatal for the SAVE, but never silent. A tripwire that
        # dies quietly is worse than no tripwire: a swallowed exception here would
        # leave the check silently disabled.
        # The save proceeds either way; the failure still has to reach a human.
        logger.warning("afternoon grounding check FAILED to run", exc_info=True)
        try:
            import sentry_sdk
            sentry_sdk.capture_exception()
        except Exception:
            pass

    report_id = content_db.save_report(
        content=data['content'],
        assets_included=data.get('assets', []),
        generation_time_ms=data.get('generation_time_ms', 0),
        report_type='afternoon',
        source_briefing=_extract_source_briefing(data, 'afternoon'),
    )

    # Send ntfy notification
    try:
        ntfy_topic = current_app.config.get('NTFY_MARKET_TOPIC')
        if ntfy_topic:
            import requests as req
            req.post(f'{_ntfy_base()}/{ntfy_topic}', data=f'Afternoon delta #{report_id} ready'.encode('utf-8'), headers={
                'Title': 'Afternoon Market Update',
                'Tags': 'chart_with_upwards_trend',
                'Click': f'{site_base_url()}/report/morning',
            }, timeout=5)
    except Exception:
        pass

    return jsonify({'success': True, 'report_id': report_id})


@reports_bp.route('/api/report/general', methods=['POST'])
@csrf.exempt
@limiter.limit("5 per minute;20 per hour")
def save_general_report():
    """Save the free-tier operator-neutral "general" report.

    Mirrors the afternoon save (API-key server-to-server or admin session) but
    with no morning ntfy/Telegram/screenshot side effects — this is the light
    shared report served to free accounts via /api/report/morning/latest.
    Without this route post_report() POSTs to a non-existent /api/report/general
    and the generated report 404s on save (silently dropped).

    A non-admin session (any logged-in user, incl. free tier) is
    NOT sufficient. Only the internal engine key or an
    admin session may save.
    """
    if request.args.get('key'):
        logger.warning(
            "[SECURITY] Rejected: API key passed as URL parameter on %s. Use X-Internal-Key or X-API-Key header.",
            request.path,
        )
        return jsonify({'error': 'API key via URL parameter is deprecated. Use X-Internal-Key or X-API-Key header.'}), 400
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key):
        if 'user_id' not in session:
            return jsonify({'error': 'unauthorized'}), 401
        if not session.get('is_admin'):
            return jsonify({'error': 'unauthorized'}), 403

    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'content is required'}), 400
    if len(data['content']) > 100_000:
        return jsonify({'error': 'content too large (max 100KB)'}), 413

    # Persist metadata (dedup metrics, x_candidates) — same handling as the
    # morning save path. Without this the engine's metadata is silently dropped.
    metadata_raw = data.get('metadata')
    metadata_str = None
    if metadata_raw and isinstance(metadata_raw, dict):
        metadata_str = json.dumps(metadata_raw)
    elif metadata_raw and isinstance(metadata_raw, str):
        metadata_str = metadata_raw

    from carrac.content_db import get_content_db
    content_db = get_content_db()

    # Catches the same header-vs-body spread contradiction the afternoon
    # check does ("(spread +0.45)" vs "sits at +0.44%" in the SAME document).
    # Reuses
    # check_spread_self_consistency via check_general_grounding — not a
    # second copy of the rule. Non-fatal by design, same as the afternoon
    # save path: notify, never withhold a save.
    try:
        from carrac.reports.afternoon_grounding import check_general_grounding
        check_general_grounding(data['content'])
    except Exception:
        logger.warning("general grounding check FAILED to run", exc_info=True)
        try:
            import sentry_sdk
            sentry_sdk.capture_exception()
        except Exception:
            pass

    report_id = content_db.save_report(
        content=data['content'],
        assets_included=data.get('assets', []),
        generation_time_ms=data.get('generation_time_ms', 0),
        report_type='general',
        metadata=metadata_str,
        source_briefing=_extract_source_briefing(data, 'general'),
    )
    return jsonify({'success': True, 'report_id': report_id})


@reports_bp.route('/api/report/week_ahead', methods=['POST'])
@csrf.exempt
@limiter.limit("5 per minute;20 per hour")
def save_week_ahead_report():
    """Save the Sunday "week ahead" shared report.

    Mirrors the weekly/general save (API-key server-to-server or admin
    session). run_week_ahead() in the engine POSTs the shared report here;
    without this route it 404s on save and the generated report is silently
    dropped (the /api/report/general route needs the same save endpoint).

    A non-admin session (any logged-in user, incl. free tier) is
    NOT sufficient. Only the internal engine key or an
    admin session may save.
    """
    if request.args.get('key'):
        logger.warning(
            "[SECURITY] Rejected: API key passed as URL parameter on %s. Use X-Internal-Key or X-API-Key header.",
            request.path,
        )
        return jsonify({'error': 'API key via URL parameter is deprecated. Use X-Internal-Key or X-API-Key header.'}), 400
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key):
        if 'user_id' not in session:
            return jsonify({'error': 'unauthorized'}), 401
        if not session.get('is_admin'):
            return jsonify({'error': 'unauthorized'}), 403

    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'content is required'}), 400
    if len(data['content']) > 100_000:
        return jsonify({'error': 'content too large (max 100KB)'}), 413

    from carrac.content_db import get_content_db
    content_db = get_content_db()
    report_id = content_db.save_report(
        content=data['content'],
        assets_included=data.get('assets', []),
        generation_time_ms=data.get('generation_time_ms', 0),
        report_type='week_ahead',
        source_briefing=_extract_source_briefing(data, 'week_ahead'),
    )
    return jsonify({'success': True, 'report_id': report_id})


# ------------------------------------------------------------------
# Get Latest Afternoon Delta
# ------------------------------------------------------------------

@reports_bp.route('/api/report/afternoon/latest', methods=['GET'])
@auth_or_key_required
def get_latest_afternoon_report():
    """Get the latest afternoon delta report as JSON.

    The afternoon report references the operator's positions (the "Updated
    Action Items" section + prose) — non-operators receive the
    operator-neutral cut.
    """
    import json as _json
    from carrac.content_db import get_content_db
    content_db = get_content_db()
    report = content_db.get_latest_report(report_type='afternoon')
    if not report:
        return jsonify({'content': None}), 200
    content = _text_for_viewer(report, 'afternoon')['content'] or ''
    # Surface the structured delta from metadata (null-safe: never 500).
    delta = None
    try:
        raw_meta = report.get('metadata')
        if raw_meta:
            meta = _json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
            delta = meta.get('delta') if isinstance(meta, dict) else None
    except Exception:
        delta = None
    return jsonify({
        'content': content,
        'created_at': report.get('created_at', ''),
        'id': report.get('id'),
        'delta': delta,
    })


# ------------------------------------------------------------------
# Get Latest Weekly Report
# ------------------------------------------------------------------

@reports_bp.route('/api/report/weekly/latest', methods=['GET'])
@auth_or_key_required
def get_latest_weekly_report():
    """Get the latest weekly report as JSON.

    The weekly digest reviews the operator's own plays/trades ("Play Review",
    "What Worked / What Didn't") — non-operators receive the operator-neutral
    cut (those sections stripped + position-fingerprint lines scrubbed).
    """
    from carrac.content_db import get_content_db
    content_db = get_content_db()
    report = content_db.get_latest_report(report_type='weekly')
    if not report:
        return jsonify({'content': None}), 200
    content = _text_for_viewer(report, 'weekly')['content'] or ''
    return jsonify({
        'content': content,
        'created_at': report.get('created_at', ''),
        'id': report.get('id')
    })


@reports_bp.route('/api/report/week_ahead/latest', methods=['GET'])
@auth_or_key_required
def get_latest_week_ahead_report():
    """Get the latest week-ahead report as JSON.

    Same operator-neutral treatment as the weekly digest for non-operators.
    """
    from carrac.content_db import get_content_db
    content_db = get_content_db()
    report = content_db.get_latest_report(report_type='week_ahead')
    if not report:
        return jsonify({'content': None}), 200
    content = _text_for_viewer(report, 'week_ahead')['content'] or ''
    return jsonify({
        'content': content,
        'created_at': report.get('created_at', ''),
        'id': report.get('id')
    })


# ------------------------------------------------------------------
# View Specific Weekly Report
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Report Diff (signal changes vs previous snapshot)
# ------------------------------------------------------------------

@reports_bp.route('/api/report/diff')
@auth_or_key_required
def api_report_diff():
    """Compare current signals against previous snapshot.

    Detects consensio direction flips, RSI 30/70 crossings, SMA crossovers.
    Returns structured changes with significance levels.
    """
    db = _get_db()
    consensio = _get_consensio()

    prev_snapshot = {}
    try:
        prev_snapshot = db.load_previous_snapshot()
    except Exception:
        pass

    if not prev_snapshot:
        return jsonify({'status': 'ok', 'changes': [], 'high_count': 0, 'medium_count': 0})

    changes = []

    # Assets to check (non-ratio, non-meta keys from snapshot)
    symbols = [k for k in prev_snapshot.keys() if not k.startswith('_')]
    # This is the same registry-only rule as
    # get_morning_context's _fetch_signal_diff (this route is its legacy
    # standalone/fallback sibling, both used by "Signal Changes" consumers) —
    # see _signal_diff_registry_symbols's docstring.
    symbols = _signal_diff_registry_symbols(symbols)

    for symbol in symbols:
        prev = prev_snapshot[symbol]
        prev_1d = prev.get('signal_1d', 0)
        prev_3d = prev.get('signal_3d', 0)
        prev_1w = prev.get('signal_1w', 0)
        prev_rsi = prev.get('rsi')

        try:
            data = db.get_multi_timeframe_data(symbol)
            if not data or data.get('1D') is None or data['1D'].empty:
                continue

            result = consensio.calculate_multi_timeframe(data['1D'], data['3D'], data['1W'])
            cur_1d = result['1D']['signal']
            cur_3d = result['3D']['signal']
            cur_1w = result['1W']['signal']

            # Consensio direction flips
            if cur_1d != prev_1d:
                direction = 'bullish' if cur_1d > prev_1d else 'bearish'
                changes.append({
                    'symbol': symbol,
                    'field': 'consensio_1D',
                    'old_value': prev_1d,
                    'new_value': cur_1d,
                    'change_type': f'1D signal flip ({direction})',
                    'significance': 'high',
                })
            if cur_3d != prev_3d:
                direction = 'bullish' if cur_3d > prev_3d else 'bearish'
                changes.append({
                    'symbol': symbol,
                    'field': 'consensio_3D',
                    'old_value': prev_3d,
                    'new_value': cur_3d,
                    'change_type': f'3D signal flip ({direction})',
                    'significance': 'medium',
                })
            if cur_1w != prev_1w:
                direction = 'bullish' if cur_1w > prev_1w else 'bearish'
                changes.append({
                    'symbol': symbol,
                    'field': 'consensio_1W',
                    'old_value': prev_1w,
                    'new_value': cur_1w,
                    'change_type': f'1W signal flip ({direction})',
                    'significance': 'medium',
                })

            # RSI 30/70 crossings — canonical daily Wilder RSI,
            # both ends from this one series. The second of the two copies;
            # fixing either alone leaves the bug live on the other path.
            close = data['1D']['close']
            if len(close) >= 15:
                prev_rsi, cur_rsi = _daily_rsi_pair(close)

                if prev_rsi is not None and cur_rsi is not None:
                    # Crossed below 30 (entered oversold)
                    if cur_rsi < 30 and prev_rsi >= 30:
                        changes.append({
                            'symbol': symbol, 'field': 'rsi_14',
                            'old_value': prev_rsi, 'new_value': cur_rsi,
                            'change_type': 'RSI entered oversold (<30)',
                            'significance': 'high',
                        })
                    # Crossed above 70 (entered overbought)
                    elif cur_rsi > 70 and prev_rsi <= 70:
                        changes.append({
                            'symbol': symbol, 'field': 'rsi_14',
                            'old_value': prev_rsi, 'new_value': cur_rsi,
                            'change_type': 'RSI entered overbought (>70)',
                            'significance': 'high',
                        })
                    # Exited oversold
                    elif cur_rsi >= 30 and prev_rsi < 30:
                        changes.append({
                            'symbol': symbol, 'field': 'rsi_14',
                            'old_value': prev_rsi, 'new_value': cur_rsi,
                            'change_type': 'RSI exited oversold (crossed 30)',
                            'significance': 'medium',
                        })
                    # Exited overbought
                    elif cur_rsi <= 70 and prev_rsi > 70:
                        changes.append({
                            'symbol': symbol, 'field': 'rsi_14',
                            'old_value': prev_rsi, 'new_value': cur_rsi,
                            'change_type': 'RSI exited overbought (crossed 70)',
                            'significance': 'medium',
                        })

            # SMA 50/200 crossovers (death/golden cross — 50/200 standard)
            if len(close) >= 200:
                sma50 = close.rolling(50).mean()
                sma200 = close.rolling(200).mean()
                cur_cross = sma50.iloc[-1] > sma200.iloc[-1]
                prev_cross = sma50.iloc[-2] > sma200.iloc[-2] if len(sma50) > 1 else cur_cross
                if cur_cross != prev_cross:
                    cross_type = 'Golden Cross (SMA50 > SMA200)' if cur_cross else 'Death Cross (SMA50 < SMA200)'
                    changes.append({
                        'symbol': symbol, 'field': 'sma_crossover',
                        'old_value': 'golden' if prev_cross else 'death',
                        'new_value': 'golden' if cur_cross else 'death',
                        'change_type': cross_type,
                        'significance': 'high',
                    })

        except Exception as e:
            logger.debug(f"Diff calc failed for {symbol}: {e}")
            continue

    changes = _tag_signal_diff_asset_class(changes, db)
    high_count = sum(1 for c in changes if c['significance'] == 'high')
    medium_count = sum(1 for c in changes if c['significance'] == 'medium')

    return jsonify({
        'status': 'ok',
        'changes': changes,
        'high_count': high_count,
        'medium_count': medium_count,
    })


@reports_bp.route('/report/weekly/<int:report_id>')
@login_required
def weekly_report_view(report_id):
    """Display a specific weekly digest report."""
    from carrac.content_db import get_content_db
    content_db = get_content_db()
    report = content_db.get_report_by_id(report_id)
    if not report or report.get('report_type') != 'weekly':
        return 'Report not found', 404
    report = {**report, **_text_for_viewer(report, 'weekly')}
    return render_template('morning_report_view.html', report=report,
                           report_type='weekly',
                           age_days=_report_age_days(report))


@reports_bp.route('/report/weekly/latest')
@login_required
def weekly_report_latest_view():
    """Redirect to the most recent weekly digest (HTML view)."""
    from carrac.content_db import get_content_db
    report = get_content_db().get_latest_report(report_type='weekly')
    if not report:
        return 'No weekly report yet', 404
    return redirect(f"/report/weekly/{report['id']}")


@reports_bp.route('/report/week_ahead/<int:report_id>')
@login_required
def week_ahead_report_view(report_id):
    """Display a specific week-ahead report."""
    from carrac.content_db import get_content_db
    report = get_content_db().get_report_by_id(report_id)
    if not report or report.get('report_type') != 'week_ahead':
        return 'Report not found', 404
    report = {**report, **_text_for_viewer(report, 'week_ahead')}
    return render_template('morning_report_view.html', report=report,
                           report_type='week_ahead',
                           age_days=_report_age_days(report))


@reports_bp.route('/report/week_ahead/latest')
@login_required
def week_ahead_report_latest_view():
    """Redirect to the most recent week-ahead report (HTML view)."""
    from carrac.content_db import get_content_db
    report = get_content_db().get_latest_report(report_type='week_ahead')
    if not report:
        return 'No week-ahead report yet', 404
    return redirect(f"/report/week_ahead/{report['id']}")


# ── Credit check/debit endpoints ─────────────────

@reports_bp.route('/api/credits/check', methods=['GET'])
@csrf.exempt
def credits_check():
    """Check user's AI credit balance. Engine use only (API key auth)."""
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    user_id = request.args.get('user_id', type=int)
    if not user_id:
        return jsonify({'error': 'user_id required'}), 400

    from user_db import user_db
    balance = user_db.get_ai_balance(user_id)
    return jsonify({'user_id': user_id, 'balance_cents': balance})


@reports_bp.route('/api/credits/debit', methods=['POST'])
@csrf.exempt
def credits_debit():
    """Debit user's AI credits and log usage. Engine use only (API key auth)."""
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    data = request.get_json() or {}
    user_id = data.get('user_id')
    amount_cents = data.get('amount_cents', 0)
    feature = data.get('feature', 'morning_personal')
    model = data.get('model', '')
    input_tokens = data.get('input_tokens', 0)
    output_tokens = data.get('output_tokens', 0)

    if not user_id or amount_cents <= 0:
        return jsonify({'error': 'user_id and positive amount_cents required'}), 400

    from user_db import user_db
    success = user_db.deduct_ai_credits(user_id, amount_cents)
    if success:
        user_db.log_ai_usage(
            user_id=user_id, feature=feature, asset=None,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_cents=amount_cents, charged_cents=amount_cents,
            model=model,
        )

    return jsonify({
        'success': success,
        'user_id': user_id,
        'debited_cents': amount_cents if success else 0,
    })


@reports_bp.route('/api/engine/log-cost', methods=['POST'])
@csrf.exempt
def engine_log_cost():
    """Log morning engine cost for admin analytics. API key auth only."""
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_admin_key(api_key):
        return jsonify({'error': 'unauthorized'}), 401

    data = request.get_json() or {}
    from user_db import user_db
    user_db.log_ai_usage(
        user_id=data.get('user_id', 0),
        feature=data.get('feature', 'morning_engine'),
        asset=None,
        input_tokens=data.get('input_tokens', 0),
        output_tokens=data.get('output_tokens', 0),
        cost_cents=data.get('cost_cents', 0),
        charged_cents=0,  # platform cost, not charged to user
        model=data.get('model', ''),
    )
    # Also fold engine spend into the global daily-spend counter so the budget cap
    # + cost alert SEE report-generation cost (counting only web AI would leave
    # the cap/alert blind to ~100% of real spend).
    try:
        from carrac.ai.billing import _increment_global_spend
        _increment_global_spend(int(data.get('cost_cents', 0) or 0))
    except Exception as _ce:
        logger.warning("engine global-spend increment failed (non-fatal): %s", _ce)
    return jsonify({'success': True})
