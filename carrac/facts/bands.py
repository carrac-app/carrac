"""Deterministic editorial-report data bands: R1 (Today's Docket, Liquidity &
Credit, Breadth internals, Regime clock) + R2 (Rotation, Flows & Attention)
+ R3 (own-history event stats on the Docket — see ``carrac/facts/event_study.py``).

BULLETPROOF RULE: every band here is built by pure Python
from data Carrac already collects (quant Postgres tables — quant.events,
quant.macro, quant.zscores, quant.signals, quant.onchain, quant.sentiment —
plus the carrac.market.breadth cache and the carrac.caches RS/RRG cache) — the
LLM never touches any of this module, and never will. Missing or unavailable
data means the row (or the whole band) is OMITTED — None/[], never estimated,
never placeholdered.

Regime clock deliberately reads the SAME quant.signals regime_hmm series
that the existing regime banner (quant/api/routes.py::build_snapshot) uses,
so its state/day-count can never describe a different classifier than the
banner it's appended to in the template.

Rotation deliberately reads the SAME cached RS/RRG payload that
``/api/relative-strength`` serves (``carrac.caches.get_cached_rs()``) and
NEVER recomputes ``_compute_rs_rankings()`` itself — that function pulls
multi-timeframe OHLCV for every tracked asset; recomputing it per report
render would duplicate real DB cost that the precompute pre-warmer already
pays on a schedule (see ``carrac/precompute.py``). A cold cache (not yet
warmed) means the band is simply omitted this render, not recomputed.

In particular: the Docket carries NO consensus/expected column. Finnhub's
``/calendar/economic`` is a premium endpoint (HTTP 403 on the current plan;
see the "Forward consensus: DELETED" note in quant/collectors/event_calendar.py),
so ``quant.events.expected`` is never populated. Report prompts and this
module alike must never invent a forward consensus figure.

R3 docket history is filled from ``carrac.facts.event_study.event_next_day_
stats()`` — a SIBLING module (own bulletproof rule, own n>=8 gate), not this
one. See its docstring for the density caveat: with the current data,
EVERY high-impact event_type is expected to gate to None (fewer than 8 past
prints exist yet) — that is the n-gate working correctly on immature
history, not a bug in this wiring.

NOT provided: the "History says" base-rate line on spotlight cards
(``clusters[i]['history_says']``). Verified:
``quant/engine/conditional.py::query_conditional``'s condition vocabulary is
z-score metrics from ``quant.zscores`` (domains macro/sentiment/derivatives_*
/onchain/price_vol — see ``quant/normalize.py::DOMAIN_QUERIES``), which has
NO per-asset RSI z-score and NO per-asset range-percentile z-score for ANY
asset. The two facts-bundle fields intended as condition inputs
(``bundle['rsi']['zone']``, ``bundle['range_pctile']['pct']``) are computed
entirely in the app layer (``carrac/precompute.py`` + ``carrac/indicator_
eval.py::rsi_zone`` + ``morning_engine/ta_facts.py::price_percentile``) and
never written into ``quant.zscores`` — so neither of the two fixed
condition templates can be expressed via ``query_conditional`` as specified.
The rule applied was: if the engine cannot express a template, drop the
feature rather than improvise a third mechanism. So it is dropped here, and
two follow-up designs remain viable. The
already-shipped frontend (``card.get('history_says')``) degrades safely —
it renders nothing when the key is absent, same as any other omitted band.

Public API — each returns the PROVISIONAL CONTRACT shape
(R1 and R2 editorial bands), or None/[]
on any missing data / error. None of these ever raise.

    build_docket(conn=None, today=None)   -> list[dict] | None
    build_liquidity(conn=None)            -> list[dict] | None
    build_breadth(breadth_data=None)      -> list[dict] | None
    build_regime_clock(conn=None)         -> dict | None
    build_rotation(rs_data=None)          -> list[dict] | None
    build_flows(conn=None)                -> list[dict] | None
    build_desk(unlocked, conn=None)       -> dict | None

NO-LEAK RULE (Tier 2.2, derivatives desk paywall): ``build_desk`` is the first band whose contract has a paid
gate. When its caller passes ``unlocked=False``, every row's real value is
replaced with ``None`` EXCEPT rows explicitly marked ``free`` (the single
teaser hook per asset) — this happens ENTIRELY inside this module. Real
numbers physically cannot reach a locked render because they never leave
the builder in the first place; the template only ever sees ``None`` and
renders it as "•••". The route that calls ``build_desk`` decides
``unlocked`` (via the same tier discriminator the report personalization
splice uses — see ``carrac/reports/routes.py::_desk_unlocked``); this
module trusts that decision and never re-derives tier itself.

DESK SOURCES — COMPLIANCE:
the Deribit options complex is deliberately absent from this band
(``quant/collectors/deribit_options.py`` — ``dvol``, ``iv_7d``/``iv_30d``/
``iv_90d``, ``term_structure_slope``, ``skew_25d``, ``iv_rv_premium``,
``basis_pct``, ``gex_net``, ``gex_flip_price``, ``options_max_pain``,
``options_put_call_ratio``) is kept out of ``_DESK_ROWS`` because
Deribit's ToS bars publishing its market/derived data. The collector itself
keeps running (an internal-use question is separately with counsel), but
none of its columns may reach a rendered row — this module deliberately
excludes every ``deribit_options.py`` column. The desk is Binance-only:
``funding_rate_avg``, ``oi_usd``, ``long_short_ratio`` from
``quant/collectors/binance_daily.py`` (licence-permitted), the same
``quant.derivatives`` table keyed by (asset, ts). Restoring any Deribit
column requires written approval before it can return here.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from carrac.facts.labels import asset_name
from carrac.facts.quant_card import _global_label

log = logging.getLogger(__name__)

# Module-level imports for patchability in tests (same idiom as
# carrac/facts/correlations.py). Each name below is the connection/data
# getter for exactly one band, so tests can monkeypatch just that one to
# exercise the "reader raises" non-fatal path.
try:
    from carrac.db_pool import get_quant_conn  # noqa: F401
except ImportError:  # pragma: no cover — quant not installed in some envs
    get_quant_conn = None  # type: ignore[assignment]

try:
    from carrac.market.breadth import get_breadth  # noqa: F401
except ImportError:  # pragma: no cover
    get_breadth = None  # type: ignore[assignment]

try:
    from carrac.caches import get_cached_rs  # noqa: F401
except ImportError:  # pragma: no cover
    get_cached_rs = None  # type: ignore[assignment]

# R3 — own-history event stats for the Docket's 'history' field (see
# carrac/facts/event_study.py; same bulletproof rule + its own density
# caveat). Module-level import for patchability, same idiom as the readers
# above.
try:
    from carrac.facts.event_study import event_next_day_stats  # noqa: F401
except ImportError:  # pragma: no cover
    event_next_day_stats = None  # type: ignore[assignment]

# High-impact event_type values (quant.events) the Docket enriches with an
# own-history line. Per the plan: high-impact types only, NEVER every type
# (e.g. OPTIONS_EXPIRY/ECB/BOJ don't get a history line).
_HIGH_IMPACT_EVENT_TYPES = ("CPI", "NFP", "FOMC", "PCE", "PPI", "GDP")

# quant.engine.config.ZSCORE_ALIASES maps a short *canonical* metric name to
# the actual raw table/z-score metric name where they differ (documented
# there as "where they differ" — e.g. canonical 'GTREND_BUY_BTC' -> raw
# 'GTREND_BUY_BITCOIN'). quant_card.py's label map is keyed by the
# CANONICAL names (it labels quant/api/routes.py::build_snapshot anomalies,
# which are canonical-keyed); build_flows() below reads quant.sentiment
# directly by its RAW metric names. Inverting the alias table here (reused,
# not re-derived) bridges that one known naming gap before the label lookup.
try:
    from quant.engine.config import ZSCORE_ALIASES as _ZSCORE_ALIASES
except ImportError:  # pragma: no cover — quant not installed in some envs
    _ZSCORE_ALIASES = {}
_RAW_TO_CANONICAL_METRIC = {raw: canonical for canonical, raw in _ZSCORE_ALIASES.items()}


# ══════════════════════════════════════════════════════════════════════════
# Today's Docket
# ══════════════════════════════════════════════════════════════════════════

def _format_event_history(event_type: str, stats: dict) -> str:
    """PURE. Exact contract string (R2/R3 editorial-bands contract): 'Last {n} {event_type} prints: SPX
    median absolute next-day move {x:.1f}%'. Strictly historical-descriptive
    — no consensus/expected, never predictive/advisory language."""
    return (
        f"Last {stats['n']} {event_type} prints: "
        f"SPX median absolute next-day move {stats['median_abs_move_pct']:.1f}%"
    )


def _shape_docket(rows: list[dict], event_stats: Optional[dict] = None) -> list[dict]:
    """PURE: shape raw ``quant.events`` rows into the contract's docket rows.

    Chronological (ascending by ``when``). NEVER includes an 'expected'/
    consensus key — see module docstring bulletproof rule.

    ``event_stats`` (R3): optional {event_type: {'n', 'median_abs_move_pct'}}
    map (see carrac/facts/event_study.py::event_next_day_stats) — a row's
    'history' is filled via _format_event_history() when its (uppercased)
    event_type has an entry, else None. Omitted/None -> every row's history
    is None (back-compat with R1 callers that never pass this).
    """
    _stats = event_stats or {}
    out = []
    for r in rows:
        ts = r.get("ts")
        when = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        # Normalize to lowercase 'high'|'medium'|'low' — the impact-chip CSS
        # class binding in the partial assumes lowercase regardless of how
        # the events table happens to store it.
        impact = (r.get("impact") or "").strip().lower() or None
        event_type = (r.get("event_type") or "").strip().upper()
        row_stats = _stats.get(event_type)
        history = _format_event_history(event_type, row_stats) if row_stats else None
        out.append({
            "when": when,
            "title": r.get("title"),
            "impact": impact,
            "actual": r.get("actual"),
            "history": history,
        })
    out.sort(key=lambda row: row["when"])
    return out


def _fetch_docket_rows(conn, start, end) -> list[dict]:
    """Execute the docket window query against an already-open connection.

    Selects ts/event_type/title/impact/actual — NEVER `expected` (bulletproof
    rule). `event_type` is used only internally (R3 history lookup — see
    _shape_docket); it is never surfaced as a docket row key itself.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT ts, event_type, title, impact, actual FROM quant.events "
        "WHERE ts >= %s AND ts <= %s ORDER BY ts",
        (start, end),
    )
    return [dict(r) for r in cur.fetchall()]


def build_docket(conn=None, today=None) -> Optional[list[dict]]:
    """Today's Docket: quant.events from yesterday through +7 days, chronological.

    R3: rows whose event_type is high-impact (_HIGH_IMPACT_EVENT_TYPES) AND
    present in this window get their 'history' field filled from
    carrac.facts.event_study.event_next_day_stats() — one lookup per
    DISTINCT type actually in the window (never all 6 unconditionally), each
    on its own DB connection (non-fatal; see event_study's docstring for the
    density caveat: today, every type is expected to gate to None
    on real data — that's the n>=8 gate working, not a bug).

    Parameters
    ----------
    conn  : optional pre-existing DB connection (quant Postgres); when None,
            obtains one via carrac.db_pool.get_quant_conn() (used as a context manager).
    today : optional override for "today" (a date); defaults to UTC today.

    Returns None on any error, or when the window has zero events (band
    omitted per the Global Constraints — never render an empty band).
    """
    try:
        _today = today or datetime.now(timezone.utc).date()
        start = _today - timedelta(days=1)
        end = _today + timedelta(days=7)

        if conn is not None:
            raw = _fetch_docket_rows(conn, start, end)
        else:
            with get_quant_conn() as c:
                raw = _fetch_docket_rows(c, start, end)

        event_stats: dict = {}
        present_types = {
            (r.get("event_type") or "").strip().upper() for r in raw
        } & set(_HIGH_IMPACT_EVENT_TYPES)
        for event_type in present_types:
            stats = event_next_day_stats(event_type)
            if stats is not None:
                event_stats[event_type] = stats

        rows = _shape_docket(raw, event_stats)
        return rows or None
    except Exception as exc:  # noqa: BLE001
        log.warning("build_docket failed (non-fatal): %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════
# Liquidity & Credit
# ══════════════════════════════════════════════════════════════════════════

# Curated FRED set, fixed order (the planned list: VIX, HY spread, RRP, TGA,
# M2, jobless claims, breakevens). metric keys match quant.collectors.fred_macro
# .FRED_SERIES; entries absent from quant.macro (no raw value yet) are skipped
# — never invented. Formatter failures also skip the row (non-fatal).
_LIQUIDITY_SERIES = [
    ("VIX", "VIX", lambda v: f"{v:.1f}"),
    ("HY_SPREAD", "High-yield spread", lambda v: f"{v:.2f}%"),
    ("REVERSE_REPO", "Reverse repo (Fed)", lambda v: f"${v:,.0f}B"),
    ("TGA", "Treasury General Account", lambda v: f"${v:,.0f}B"),
    ("M2", "M2 money supply", lambda v: f"${v / 1000:,.1f}T"),
    ("INITIAL_CLAIMS", "Initial jobless claims", lambda v: f"{v / 1000:,.0f}K"),
    ("BREAKEVEN_10Y", "10Y inflation breakeven", lambda v: f"{v:.2f}%"),
]


def _percentile_meaning(p: float) -> str:
    """Phrase a 0-100 percentile as bottom/top-N% of the trailing window —
    NEVER the inverted "lower/higher than N%" (a p11 VIX must read
    "bottom 11%", not "lower than 11%"). Percentile p means the value exceeds p% of the window's history.

    Extracted from ``_shape_liquidity`` (R1) so R2's ``_shape_flows`` reuses
    the exact same phrasing rule rather than re-deriving it."""
    if p <= 50:
        return f"in the bottom {max(p, 1):.0f}% of the last 90 days"
    return f"in the top {max(100 - p, 1):.0f}% of the last 90 days"


def _shape_liquidity(macro_rows: dict, zscore_rows: dict) -> list[dict]:
    """PURE. ``macro_rows``: {metric: raw_value}. ``zscore_rows``: {metric:
    (zscore, percentile)}. Order fixed by _LIQUIDITY_SERIES; a metric absent
    from macro_rows (no raw value) is skipped — never invented."""
    out = []
    for metric, label, fmt in _LIQUIDITY_SERIES:
        raw = macro_rows.get(metric)
        if raw is None:
            continue
        try:
            value_str = fmt(float(raw))
        except Exception:  # noqa: BLE001
            continue

        zscore, percentile = zscore_rows.get(metric, (None, None))
        meaning = _percentile_meaning(float(percentile)) if percentile is not None else None

        out.append({
            "label": label,
            "value": value_str,
            "zscore": round(float(zscore), 2) if zscore is not None else None,
            "percentile": float(percentile) if percentile is not None else None,
            "meaning": meaning,
        })
    return out


def _fetch_liquidity_rows(conn, metrics: list[str]):
    """Execute the two liquidity queries (latest raw value + latest 90d
    z-score/percentile per metric) against an already-open connection."""
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT ON (metric) metric, value FROM quant.macro "
        "WHERE metric = ANY(%s) ORDER BY metric, ts DESC",
        (metrics,),
    )
    macro_rows = {r["metric"]: r["value"] for r in cur.fetchall()}

    cur.execute(
        "SELECT DISTINCT ON (metric) metric, zscore, percentile FROM quant.zscores "
        "WHERE domain = 'macro' AND window_days = 90 AND asset = '_GLOBAL_' "
        "AND metric = ANY(%s) ORDER BY metric, ts DESC",
        (metrics,),
    )
    zscore_rows = {
        r["metric"]: (
            r.get("zscore") if r.get("zscore") is None else float(r["zscore"]),
            r.get("percentile") if r.get("percentile") is None else float(r["percentile"]),
        )
        for r in cur.fetchall()
    }
    return macro_rows, zscore_rows


def build_liquidity(conn=None) -> Optional[list[dict]]:
    """Liquidity & Credit: curated FRED set (see _LIQUIDITY_SERIES), each row's
    value from quant.macro (latest print) and zscore/percentile from
    quant.zscores (latest 90-day window) when available.

    Parameters
    ----------
    conn : optional pre-existing DB connection (quant Postgres); when None,
           obtains one via carrac.db_pool.get_quant_conn() (used as a context manager).

    Returns None on any error, or when every curated series is missing
    (band omitted).
    """
    try:
        metrics = [m for m, _, _ in _LIQUIDITY_SERIES]

        if conn is not None:
            macro_rows, zscore_rows = _fetch_liquidity_rows(conn, metrics)
        else:
            with get_quant_conn() as c:
                macro_rows, zscore_rows = _fetch_liquidity_rows(c, metrics)

        rows = _shape_liquidity(macro_rows, zscore_rows)
        return rows or None
    except Exception as exc:  # noqa: BLE001
        log.warning("build_liquidity failed (non-fatal): %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════
# Breadth internals
# ══════════════════════════════════════════════════════════════════════════

def _section_pct(section) -> Optional[float]:
    if not section:
        return None
    return section.get("pct")


def _shape_breadth(data: dict) -> list[dict]:
    """PURE. Shapes carrac.market.breadth.get_breadth() output into the
    contract's small fixed row set. tone: warm/cold reuse the existing
    hot/cold color convention (What's-stretched / heat strip); None when
    a row isn't extreme enough to lean either way.

    An empty/uncovered breadth snapshot (covered == 0) → [] (band omitted).
    """
    if not data or not data.get("covered"):
        return []

    rows = []

    above200 = _section_pct(data.get("above_sma200"))
    if above200 is not None:
        rows.append({
            "label": "Above 200-day",
            "value": f"{above200:.0f}%",
            "tone": "warm" if above200 >= 50 else "cold",
        })

    above50 = _section_pct(data.get("above_sma50"))
    if above50 is not None:
        rows.append({
            "label": "Above 50-day",
            "value": f"{above50:.0f}%",
            "tone": "warm" if above50 >= 50 else "cold",
        })

    oversold = _section_pct(data.get("oversold"))
    if oversold is not None:
        rows.append({
            "label": "Oversold (RSI < 30)",
            "value": f"{oversold:.0f}%",
            "tone": "cold" if oversold >= 20 else None,
        })

    overbought = _section_pct(data.get("overbought"))
    if overbought is not None:
        rows.append({
            "label": "Overbought (RSI > 70)",
            "value": f"{overbought:.0f}%",
            "tone": "warm" if overbought >= 25 else None,
        })

    highs = (data.get("new_20d_highs") or {}).get("count")
    lows = (data.get("new_20d_lows") or {}).get("count")
    if highs is not None and lows is not None:
        tone = "warm" if highs > lows else ("cold" if lows > highs else None)
        rows.append({
            "label": "New 20-day highs vs lows",
            "value": f"{highs} / {lows}",
            "tone": tone,
        })

    advancers = data.get("advancers")
    decliners = data.get("decliners")
    if advancers is not None and decliners is not None:
        tone = "warm" if advancers > decliners else ("cold" if decliners > advancers else None)
        rows.append({
            "label": "Advancers vs decliners",
            "value": f"{advancers} / {decliners}",
            "tone": tone,
        })

    return rows


def build_breadth(breadth_data=None) -> Optional[list[dict]]:
    """Breadth internals: above-200D/50D %, oversold/overbought %, new highs
    vs lows, advancers vs decliners — from carrac.market.breadth.get_breadth()
    (cached; computed fresh only on a cold cache).

    Parameters
    ----------
    breadth_data : optional pre-fetched breadth dict (for tests); when None,
                   calls carrac.market.breadth.get_breadth().

    Returns None on any error, or when the universe is uncovered (band
    omitted).
    """
    try:
        data = breadth_data if breadth_data is not None else get_breadth()
        rows = _shape_breadth(data)
        return rows or None
    except Exception as exc:  # noqa: BLE001
        log.warning("build_breadth failed (non-fatal): %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════
# Regime clock
# ══════════════════════════════════════════════════════════════════════════

# Max lookback in days for the empirical run-length history — matches the
# 730-day cap already used elsewhere for this same regime_hmm series (see
# carrac/mcp/tools/regime.py::quant_get_regime_history). Bounded read; plenty for a
# run-length median without scanning the whole table.
_REGIME_HISTORY_ROW_LIMIT = 730


def _compute_regime_clock(day_rows: list[dict]) -> Optional[dict]:
    """PURE. ``day_rows``: ASCENDING [{'date', 'regime'}, ...], one row per
    calendar day. Builds contiguous same-regime runs; the LAST run is the
    current (ongoing) one. ``median_run_days`` is the median of COMPLETED
    prior runs — None when fewer than 3 completed runs exist (not enough
    history for a meaningful median; per the plan's <3-runs rule).

    Returns None when day_rows is empty (band omitted).
    """
    if not day_rows:
        return None

    runs: list[list] = []  # [regime, length] pairs, in chronological order
    for row in day_rows:
        regime = row["regime"]
        if runs and runs[-1][0] == regime:
            runs[-1][1] += 1
        else:
            runs.append([regime, 1])

    state, day_in_state = runs[-1]
    completed = [length for _, length in runs[:-1]]

    median_run_days = None
    note = None
    if len(completed) >= 3:
        median_run_days = int(round(statistics.median(completed)))
        if day_in_state > median_run_days:
            note = "longer than the median run"
        elif day_in_state < median_run_days:
            note = "shorter than the median run"
        else:
            note = "at the median run length"

    return {
        "state": state,
        "day_in_state": day_in_state,
        "median_run_days": median_run_days,
        "note": note,
    }


def _fetch_regime_hmm_rows(conn, limit: int) -> list[dict]:
    """Execute the bounded quant.signals regime_hmm history read against an
    already-open connection. Returns rows newest-first (caller must reverse
    for chronological order).

    SAME table/classifier as the existing 'regime' banner variable in
    carrac/reports/routes.py (both read source='regime_hmm', metric='REGIME',
    asset='_GLOBAL_' from quant.signals) — deliberately, so regime_clock can
    never describe a different classifier/state-vocabulary than the banner
    it's appended to. metadata.label is the same 'favorable'/'unfavorable'
    string quant/api/routes.py::build_snapshot surfaces as regime['state'].
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT ts, metadata FROM quant.signals "
        "WHERE source = 'regime_hmm' AND metric = 'REGIME' AND asset = '_GLOBAL_' "
        "ORDER BY ts DESC LIMIT %s",
        (limit,),
    )
    out = []
    for r in cur.fetchall():
        meta = r.get("metadata") or {}
        if isinstance(meta, str):
            import json as _json  # noqa: PLC0415
            meta = _json.loads(meta)
        label = meta.get("label")
        ts = r.get("ts")
        day = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        if label:
            out.append({"date": day, "regime": label})
    return out


def build_regime_clock(conn=None) -> Optional[dict]:
    """Regime clock: current 2-state HMM regime label ('favorable' /
    'unfavorable' — the SAME classifier as the existing regime banner, see
    quant/engine/regime.py) plus how many days it has held and how that
    compares to the empirical median run length, from the daily
    quant.signals history of that same series.

    Parameters
    ----------
    conn : optional pre-existing DB connection (quant Postgres); when None,
           obtains one via carrac.db_pool.get_quant_conn() (used as a context manager).

    Returns None on any error, or when the regime_hmm series has no rows
    (band omitted).
    """
    try:
        if conn is not None:
            raw = _fetch_regime_hmm_rows(conn, _REGIME_HISTORY_ROW_LIMIT)
        else:
            with get_quant_conn() as c:
                raw = _fetch_regime_hmm_rows(c, _REGIME_HISTORY_ROW_LIMIT)

        raw.reverse()  # DESC → ascending chronological (one row per day already)
        return _compute_regime_clock(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("build_regime_clock failed (non-fatal): %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════
# Rotation (R2) — RRG quadrant strip
# ══════════════════════════════════════════════════════════════════════════

# Fixed board order (the classic RRG clock reading direction) + the mapping
# from the reader's lowercase quadrant vocabulary (carrac/charts/routes.py
# ::_compute_rs_rankings) to the contract's capitalized labels.
_ROTATION_QUADRANTS = [
    ("leading", "Leading"),
    ("improving", "Improving"),
    ("weakening", "Weakening"),
    ("lagging", "Lagging"),
]


def _shape_rotation(rs_rows: list[dict]) -> list[dict]:
    """PURE. ``rs_rows``: the cached RS/RRG payload — a list of dicts each
    with (at least) 'symbol' and 'quadrant' (lowercase 'leading'/'improving'/
    'weakening'/'lagging'). Groups into the fixed contract order; a quadrant
    with zero assets is OMITTED entirely (never an empty shell). A row whose
    quadrant value doesn't match the known vocabulary is skipped — never
    guessed. Within a quadrant, the cache's existing order (rs_score
    descending) is preserved."""
    buckets: dict[str, list[dict]] = {key: [] for key, _ in _ROTATION_QUADRANTS}
    for row in rs_rows:
        quadrant = (row.get("quadrant") or "").strip().lower()
        symbol = row.get("symbol")
        if not symbol or quadrant not in buckets:
            continue
        buckets[quadrant].append({"ticker": symbol, "name": asset_name(symbol)})

    return [
        {"quadrant": label, "assets": buckets[key]}
        for key, label in _ROTATION_QUADRANTS
        if buckets[key]
    ]


def build_rotation(rs_data=None) -> Optional[list[dict]]:
    """Rotation strip: RRG quadrant grouping, read from the SAME cache
    ``/api/relative-strength`` serves (``carrac.caches.get_cached_rs()``) —
    NEVER recomputed here (see module docstring).

    Parameters
    ----------
    rs_data : optional pre-fetched RS/RRG rankings list (for tests); when
              None, calls carrac.caches.get_cached_rs().

    Returns None on any error, when the cache is cold (None/empty — not
    recomputed), or when every quadrant is empty (band omitted).
    """
    try:
        data = rs_data if rs_data is not None else get_cached_rs()
        if not data:
            return None
        # This reads the SAME cache the RS table does, and that
        # cache is computed over the RS BENCHMARK set — which includes the
        # operator's watchlist-only symbols. The RS
        # table is filtered the same way; an unfiltered watchlist symbol would
        # show up in this strip on the public /brief, rendered as a bare ticker
        # because it has no registry display name. Filtered HERE rather than in _shape_rotation
        # so that function stays pure (see its docstring).
        from carrac.assets.registry import public_symbol_allowlist
        allowed = public_symbol_allowlist()
        if allowed is not None:
            kept = [r for r in data
                    if str((r or {}).get("symbol", "")).upper() in allowed]
            dropped = len(data) - len(kept)
            if dropped:
                log.info("Rotation: dropped %d non-registry symbol(s)", dropped)
            data = kept
        rows = _shape_rotation(data)
        return rows or None
    except Exception as exc:  # noqa: BLE001
        log.warning("build_rotation failed (non-fatal): %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════
# Flows & Attention (R2)
# ══════════════════════════════════════════════════════════════════════════

# quant.sentiment metrics for the stablecoin pair (both asset='_GLOBAL_').
_STABLECOIN_METRICS = ["STABLECOIN_DOMINANCE", "STABLECOIN_USDT_SUPPLY"]


def _fmt_signed_musd(value: float) -> str:
    """Signed dollar-millions, e.g. 142.0 -> '+$142M', -85.5 -> '-$86M'."""
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.0f}M"


def _shape_flows(data: dict) -> list[dict]:
    """PURE. ``data`` matches the ``_fetch_flows_rows`` bundle shape below.
    Fixed row order: BTC ETF net flow (day), BTC ETF net flow (5-day sum),
    stablecoin dominance, USDT supply, top-|z| Google Trends row. A missing
    or malformed series is skipped — never invented (bulletproof rule)."""
    out = []

    # ── BTC ETF net flow: latest day + trailing 5-day sum ──────────────
    etf_recent = data.get("etf_recent") or []
    if etf_recent:
        try:
            latest = float(etf_recent[0])
        except Exception:  # noqa: BLE001
            latest = None
        if latest is not None:
            zscore, percentile = data.get("etf_zscore") or (None, None)
            meaning = _percentile_meaning(float(percentile)) if percentile is not None else None
            out.append({
                "label": "BTC ETF net flow (day)",
                "value": _fmt_signed_musd(latest),
                "zscore": round(float(zscore), 2) if zscore is not None else None,
                "percentile": float(percentile) if percentile is not None else None,
                "meaning": meaning,
            })

        try:
            total = sum(float(v) for v in etf_recent)
            out.append({
                "label": "BTC ETF net flow (5-day)",
                "value": _fmt_signed_musd(total),
                "zscore": None,
                "percentile": None,
                "meaning": None,
            })
        except Exception:  # noqa: BLE001
            pass

    # ── Stablecoin dominance (%) + USDT supply ($B) ────────────────────
    stable_raw = data.get("stable_raw") or {}
    stable_z = data.get("stable_z") or {}

    dom_raw = stable_raw.get("STABLECOIN_DOMINANCE")
    if dom_raw is not None:
        try:
            value_str = f"{float(dom_raw):.1f}%"
        except Exception:  # noqa: BLE001
            value_str = None
        if value_str is not None:
            zscore, percentile = stable_z.get("STABLECOIN_DOMINANCE", (None, None))
            meaning = _percentile_meaning(float(percentile)) if percentile is not None else None
            out.append({
                "label": "Stablecoin dominance",
                "value": value_str,
                "zscore": round(float(zscore), 2) if zscore is not None else None,
                "percentile": float(percentile) if percentile is not None else None,
                "meaning": meaning,
            })

    supply_raw = stable_raw.get("STABLECOIN_USDT_SUPPLY")
    if supply_raw is not None:
        try:
            value_str = f"${float(supply_raw) / 1e9:,.1f}B"
        except Exception:  # noqa: BLE001
            value_str = None
        if value_str is not None:
            zscore, percentile = stable_z.get("STABLECOIN_USDT_SUPPLY", (None, None))
            meaning = _percentile_meaning(float(percentile)) if percentile is not None else None
            out.append({
                "label": "USDT supply",
                "value": value_str,
                "zscore": round(float(zscore), 2) if zscore is not None else None,
                "percentile": float(percentile) if percentile is not None else None,
                "meaning": meaning,
            })

    # ── Top-|z| Google Trends row (any GTREND_* metric, asset=_GLOBAL_) ─
    gtrend_raw = data.get("gtrend_raw") or {}
    gtrend_z = data.get("gtrend_z") or {}
    _candidates = sorted(
        (
            (metric, z, p)
            for metric, (z, p) in gtrend_z.items()
            if z is not None and metric in gtrend_raw
        ),
        key=lambda t: (abs(float(t[1])), t[0]),
        reverse=True,
    )
    for metric, zscore, percentile in _candidates:
        try:
            value_str = f"{float(gtrend_raw[metric]):.0f}"
        except Exception:  # noqa: BLE001
            continue
        meaning = _percentile_meaning(float(percentile)) if percentile is not None else None
        label_key = _RAW_TO_CANONICAL_METRIC.get(metric, metric)
        out.append({
            "label": _global_label(label_key),
            "value": value_str,
            "zscore": round(float(zscore), 2),
            "percentile": float(percentile) if percentile is not None else None,
            "meaning": meaning,
        })
        break  # only the single strongest-signal row

    return out


def _fetch_flows_rows(conn) -> dict:
    """Execute the flows-band queries against an already-open connection:
    BTC ETF net flow (quant.onchain, latest 5 days) + its 90d z-score
    (quant.zscores, domain='onchain'); stablecoin dominance/supply
    (quant.sentiment) + their 90d z-scores (domain='sentiment'); every
    GTREND_* search-trend metric (quant.sentiment) + its 90d z-score."""
    cur = conn.cursor()

    cur.execute(
        "SELECT value FROM quant.onchain WHERE metric = 'ETF_BTC_NET_FLOW' "
        "AND asset = 'BTC' ORDER BY ts DESC LIMIT 5"
    )
    etf_recent = [r["value"] for r in cur.fetchall()]

    cur.execute(
        "SELECT zscore, percentile FROM quant.zscores WHERE domain = 'onchain' "
        "AND metric = 'ETF_BTC_NET_FLOW' AND asset = 'BTC' AND window_days = 90 "
        "ORDER BY ts DESC LIMIT 1"
    )
    etf_z_row = cur.fetchone()
    etf_zscore = (etf_z_row["zscore"], etf_z_row["percentile"]) if etf_z_row else (None, None)

    cur.execute(
        "SELECT DISTINCT ON (metric) metric, value FROM quant.sentiment "
        "WHERE metric = ANY(%s) AND asset = '_GLOBAL_' ORDER BY metric, ts DESC",
        (_STABLECOIN_METRICS,),
    )
    stable_raw = {r["metric"]: r["value"] for r in cur.fetchall()}

    cur.execute(
        "SELECT DISTINCT ON (metric) metric, zscore, percentile FROM quant.zscores "
        "WHERE domain = 'sentiment' AND window_days = 90 AND asset = '_GLOBAL_' "
        "AND metric = ANY(%s) ORDER BY metric, ts DESC",
        (_STABLECOIN_METRICS,),
    )
    stable_z = {r["metric"]: (r["zscore"], r["percentile"]) for r in cur.fetchall()}

    # '~' (POSIX regex) rather than LIKE — sidesteps LIKE's own '_' wildcard
    # meaning, which would otherwise need escaping to match a literal '_'.
    cur.execute(
        "SELECT DISTINCT ON (metric) metric, value FROM quant.sentiment "
        "WHERE metric ~ '^GTREND_' AND asset = '_GLOBAL_' ORDER BY metric, ts DESC"
    )
    gtrend_raw = {r["metric"]: r["value"] for r in cur.fetchall()}

    cur.execute(
        "SELECT DISTINCT ON (metric) metric, zscore, percentile FROM quant.zscores "
        "WHERE domain = 'sentiment' AND window_days = 90 AND asset = '_GLOBAL_' "
        "AND metric ~ '^GTREND_' ORDER BY metric, ts DESC"
    )
    gtrend_z = {r["metric"]: (r["zscore"], r["percentile"]) for r in cur.fetchall()}

    return {
        "etf_recent": etf_recent,
        "etf_zscore": etf_zscore,
        "stable_raw": stable_raw,
        "stable_z": stable_z,
        "gtrend_raw": gtrend_raw,
        "gtrend_z": gtrend_z,
    }


def build_flows(conn=None) -> Optional[list[dict]]:
    """Flows & Attention: BTC ETF net flow (day + 5-day sum), stablecoin
    dominance + USDT supply, and the strongest-|z| Google Trends search-
    interest row — each with its 90-day z-score/percentile when available.

    Parameters
    ----------
    conn : optional pre-existing DB connection (quant Postgres); when None,
           obtains one via carrac.db_pool.get_quant_conn() (used as a context manager).

    Returns None on any error, or when every series is missing (band
    omitted).
    """
    try:
        if conn is not None:
            data = _fetch_flows_rows(conn)
        else:
            with get_quant_conn() as c:
                data = _fetch_flows_rows(c)

        rows = _shape_flows(data)
        return rows or None
    except Exception as exc:  # noqa: BLE001
        log.warning("build_flows failed (non-fatal): %s", exc)
        return None


# ══════════════════════════════════════════════════════════════════════════
# Derivatives Desk (Tier 2.2) — the first PAID-gated band. See the module
# docstring's NO-LEAK RULE and DESK SOURCES paragraphs before touching
# anything below. Binance-only (compliance) — no
# deribit_options.py column may appear anywhere in this section.
# ══════════════════════════════════════════════════════════════════════════

_DESK_TICKERS = ("BTC", "ETH")

# quant.derivatives columns the desk's fixed row set needs, beyond ts/asset —
# all three written by quant/collectors/binance_daily.py (licence-permitted).
# No Deribit column belongs in this tuple.
_DESK_COLUMNS = ("funding_rate_avg", "oi_usd", "long_short_ratio")


def _fmt_desk_col(column: str, fmt):
    """Build a single-column row formatter: reads ``row[column]``, returns
    None (row skipped — NULL/unparseable, never invented) or the formatted
    string. Shared by every _DESK_ROWS entry."""
    def _formatter(row: dict) -> Optional[str]:
        v = row.get(column)
        if v is None:
            return None
        try:
            return fmt(float(v))
        except Exception:  # noqa: BLE001
            return None
    return _formatter


# Fixed row order (compliance — see the module
# docstring's DESK SOURCES paragraph), skip-if-NULL: funding rate (free) ·
# futures OI · long/short ratio. Each entry: (label, hint, free,
# formatter(row) -> str|None). Every column is written by
# quant/collectors/binance_daily.py (licence-permitted) — no
# deribit_options.py column may be added back to this tuple without written
# approval. Formats are pinned by tests/test_facts_bands.py
# (hand-computed): funding rate as signed basis points, OI/ratio following
# the house convention already used elsewhere for these same columns.
_DESK_ROWS = (
    (
        "Funding rate",
        "average perpetual funding across the day — positive means longs "
        "pay shorts (Binance)",
        True, _fmt_desk_col("funding_rate_avg", lambda v: f"{v * 1e4:+.1f} bp"),
    ),
    (
        "Futures OI", "open interest in USD (Binance)", False,
        _fmt_desk_col("oi_usd", lambda v: f"${v / 1e9:,.2f}B"),
    ),
    (
        "Long/short ratio", "Binance account long/short ratio", False,
        _fmt_desk_col("long_short_ratio", lambda v: f"{v:.2f}"),
    ),
)


def _is_desk_stale(ts, now: Optional[datetime] = None) -> bool:
    """True when ``ts`` (quant.derivatives' ``ts`` DATE column) is more than
    48h old — the Global Constraints' staleness gate ("the desk row must be
    <=48h old ... else omitted, never stale-silently").

    ``ts`` has day granularity (no time-of-day). It is treated as MIDNIGHT
    UTC of that calendar day — the conservative direction: a row from
    "today" is measured as up to ~24h old even though it may have landed
    minutes ago, and a row from 2 calendar days back is measured as 48-72h
    old (definitely stale) even though the naive day-count is only "2". This
    can only call a row MORE stale than it really is, never less — matching
    "never stale-silently" rather than accidentally under-counting age.

    None is treated as stale (no data -> can't be fresh).
    """
    if ts is None:
        return True
    _now = now if now is not None else datetime.now(timezone.utc)
    if isinstance(ts, datetime):
        ts_dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    else:
        ts_dt = datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)
    return (_now - ts_dt) > timedelta(hours=48)


def _shape_desk_asset(ticker: str, row: dict, unlocked: bool) -> Optional[dict]:
    """PURE. Shapes one asset's raw quant.derivatives row into the contract's
    row list, in the FIXED _DESK_ROWS order.

    Two DIFFERENT kinds of "missing" here, per the no-leak rule:
      * a NULL/unparseable column -> the row is SKIPPED entirely (never
        appears, not even redacted) — same "never invented" rule every
        other band in this module follows.
      * real data exists but the caller isn't ``unlocked`` -> the row DOES
        appear (label + hint), but its value is forced to None UNLESS the
        row is marked ``free`` (the single paid-teaser hook per asset) — the
        template renders a None value as "•••". This redaction happens
        HERE, never in the template/CSS (the no-leak rule).

    Returns None when zero rows resolve (asset omitted, never an empty shell).
    """
    rows = []
    for label, hint, free, formatter in _DESK_ROWS:
        value = formatter(row)
        if value is None:
            continue
        if not unlocked and not free:
            value = None
        rows.append({"label": label, "value": value, "hint": hint, "free": free})
    if not rows:
        return None
    return {"ticker": ticker, "name": asset_name(ticker), "rows": rows}


def _fetch_desk_rows(conn, tickers) -> dict:
    """Execute the desk's ONE wide quant.derivatives read (latest row per
    asset, DISTINCT ON) against an already-open connection — never a
    per-asset N+1. Returns {asset: row_dict} (row_dict includes 'ts')."""
    cur = conn.cursor()
    cols = ", ".join(_DESK_COLUMNS)
    cur.execute(
        f"SELECT DISTINCT ON (asset) asset, ts, {cols} FROM quant.derivatives "
        "WHERE asset = ANY(%s) ORDER BY asset, ts DESC",
        (list(tickers),),
    )
    return {r["asset"]: dict(r) for r in cur.fetchall()}


def build_desk(unlocked: bool, conn=None) -> Optional[dict]:
    """Derivatives Desk (Tier 2.2): BTC + ETH funding rate, futures OI,
    long/short ratio — Binance-only (see the module docstring's DESK SOURCES
    paragraph; the Deribit options complex is excluded) — from
    the latest quant.derivatives row per asset.

    NO-LEAK RULE: see the module docstring. ``unlocked`` is a caller-
    resolved decision (the ROUTE's job — see
    carrac/reports/routes.py::_desk_unlocked) that this function trusts
    outright; it never re-derives tier itself.

    STALENESS GATE: an asset whose latest row's ``ts`` is more than 48h old
    (see ``_is_desk_stale``) is dropped from ``assets`` entirely — never
    shown stale-silently. If BOTH assets are stale/missing/errored, the
    whole band is omitted (returns None), same as when every column is
    NULL.

    Parameters
    ----------
    unlocked : whether the caller may see real values (route-resolved).
    conn     : optional pre-existing DB connection (quant Postgres); when
               None, obtains one via carrac.db_pool.get_quant_conn() (used as a context
               manager).

    Returns None on any error, or when neither BTC nor ETH resolves a fresh,
    non-empty row (band omitted — never an empty shell).
    """
    try:
        if conn is not None:
            raw = _fetch_desk_rows(conn, _DESK_TICKERS)
        else:
            with get_quant_conn() as c:
                raw = _fetch_desk_rows(c, _DESK_TICKERS)

        assets = []
        latest_ts = None
        for ticker in _DESK_TICKERS:
            row = raw.get(ticker)
            if not row or _is_desk_stale(row.get("ts")):
                continue
            asset = _shape_desk_asset(ticker, row, unlocked)
            if asset is None:
                continue
            assets.append(asset)
            ts = row.get("ts")
            if ts is not None and (latest_ts is None or ts > latest_ts):
                latest_ts = ts

        if not assets:
            return None

        as_of = latest_ts.isoformat() if hasattr(latest_ts, "isoformat") else str(latest_ts)
        return {
            "as_of": as_of,
            "unlocked": bool(unlocked),
            "cta_href": "",
            "assets": assets,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("build_desk failed (non-fatal): %s", exc)
        return None
