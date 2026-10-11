"""Deterministic, facts-only report blocks for the morning and afternoon engines.

These renderers produce markdown from live data — IN CODE, never via the LLM.
Each is NON-ADVICE (no buy/sell/should/enter/target language).

Public API:
  build_cot_block(api=None)            -> str  (G1: morning general report)
  build_actuals_block(today_events)    -> str  (G2a: afternoon)
  build_anomaly_pulse_block(api=None)  -> str  (G2b: afternoon)

today_events item keys (from quant.events via /api/quant/events/fill-actuals):
  id, ts, event_type, title, impact, expected, actual, actual_filled_at

anomalies item keys (from quant.zscores via build_snapshot):
  domain, metric, asset, zscore, percentile, value, date
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)


def _default_api() -> Callable:
    from morning_engine.data_assembly import _api
    return _api


# ──────────────────────────────────────────────────────────────────────────────
# G1 — COT Positioning block (morning general report)
# ──────────────────────────────────────────────────────────────────────────────

def build_cot_block(api: Optional[Callable] = None) -> str:
    """Render a COT positioning summary from /api/quant/cot.

    Returns "" when data is missing or the series is empty.
    Non-advice: shows z-scores / percentiles / WoW change only.
    Compact: heading + up to 6 rows, stretched rows first.
    """
    _api = api if api is not None else _default_api()
    try:
        data = _api('/api/quant/cot', timeout=12, retries=1)
    except Exception as e:
        log.warning("build_cot_block: API call failed: %s", e)
        return ""

    if not data or not isinstance(data, dict):
        return ""

    series = data.get('series') or []
    if not series:
        return ""

    data_date = data.get('data_date', '')
    stretched_count = data.get('stretched_count', 0)

    # Stretched rows first, then remaining sorted by |zscore| desc; cap at 6
    stretched_rows = [r for r in series if r.get('stretched')]
    other_rows = sorted(
        [r for r in series if not r.get('stretched')],
        key=lambda r: abs(r.get('zscore') or 0),
        reverse=True,
    )
    rows = (stretched_rows + other_rows)[:6]

    lines = [
        f"## COT Positioning — CFTC, week of {data_date} ({stretched_count} stretched)"
    ]
    for r in rows:
        market = r.get('market_label', '')
        typ = r.get('type_label', '')
        zscore = r.get('zscore') or 0
        pct = r.get('percentile') or 0
        wow = r.get('wow_change')
        stretched = r.get('stretched', False)

        parts = [f"z {zscore:+.1f}", f"{pct:.0f}th pct"]
        if wow is not None:
            parts.append(f"WoW {int(round(wow)):+d}")

        line = f"{market} · {typ}: " + " · ".join(parts)
        if stretched:
            line += " ⚠"
        lines.append(line)

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# G2a — Data Released Today actuals table (afternoon)
# ──────────────────────────────────────────────────────────────────────────────

def build_actuals_block(today_events: list) -> str:
    """Render a markdown table of today's macro events that have an actual value.

    today_events items have keys: title, event_type, impact, expected, actual.
    Returns "" when no events carry actuals.
    Non-advice: verbatim actual strings from FRED only; no opinion.
    """
    if not today_events:
        return ""

    filled = [e for e in today_events if e.get('actual') is not None]
    if not filled:
        return ""

    # Include an Expected column only when at least one row has a value
    # (Finnhub consensus is premium-gated — usually absent)
    has_expected = any(e.get('expected') is not None for e in filled)

    rows: list[str] = []
    if has_expected:
        rows.append("| Event | Impact | Actual | Expected |")
        rows.append("|-------|--------|--------|----------|")
        for e in filled:
            event_name = e.get('title') or e.get('event_type', '')
            impact = e.get('impact', '')
            actual = e.get('actual', '')
            expected = e.get('expected') or ''
            rows.append(f"| {event_name} | {impact} | {actual} | {expected} |")
    else:
        rows.append("| Event | Impact | Actual |")
        rows.append("|-------|--------|--------|")
        for e in filled:
            event_name = e.get('title') or e.get('event_type', '')
            impact = e.get('impact', '')
            actual = e.get('actual', '')
            rows.append(f"| {event_name} | {impact} | {actual} |")

    return "## Data Released Today\n" + "\n".join(rows)


# ──────────────────────────────────────────────────────────────────────────────
# G2b — Active Extremes anomaly pulse (afternoon)
# ──────────────────────────────────────────────────────────────────────────────

def _prettify_metric(metric: str, asset: str) -> str:
    """Lightly prettify a metric + asset label for display.

    When asset is meaningful, drops ANY token equal to the asset (case-insensitive)
    from the metric's token list before prettifying, then prepends the asset once.
    This handles both trailing suffixes (LS_RATIO_XRP) and embedded tokens
    (FUNDING_XRP_OKX) without duplication.
    Also applies abbreviation mapping (Ls→L/S, Oi→OI, Pc→P/C, Iv→IV, Gex→GEX).
    """
    meaningful_asset = asset and asset not in ('_GLOBAL_', '')

    # Split metric by underscore, drop tokens matching the asset (case-insensitive)
    raw_tokens = metric.split('_')
    if meaningful_asset:
        raw_tokens = [t for t in raw_tokens if t.upper() != asset.upper()]

    # Title-case each token, then join
    label = ' '.join(t.title() for t in raw_tokens if t)

    # Apply abbreviation mappings per whitespace token
    abbreviations = {'Ls': 'L/S', 'Oi': 'OI', 'Pc': 'P/C', 'Iv': 'IV', 'Gex': 'GEX'}
    tokens = label.split(' ')
    tokens = [abbreviations.get(t, t) for t in tokens]
    label = ' '.join(tokens)

    # Prepend asset if meaningful
    if meaningful_asset:
        label = f"{asset} {label}"
    return label


def build_anomaly_pulse_block(api: Optional[Callable] = None) -> str:
    """Render active extreme anomalies from /api/quant/snapshot.

    Returns "" when no anomalies are present.
    Non-advice: shows metric label / z-score / percentile only.
    Sorted by |zscore| desc, capped at 5.
    """
    _api = api if api is not None else _default_api()
    try:
        data = _api('/api/quant/snapshot')
    except Exception as e:
        log.warning("build_anomaly_pulse_block: API call failed: %s", e)
        return ""

    if not data or not isinstance(data, dict):
        return ""

    anomalies = data.get('anomalies') or []

    # This block is BAKED into the stored afternoon artifact, and
    # the snapshot call authenticates as the engine → admin → the API's
    # per-caller gate is a no-op. Strip Deribit-backed anomalies
    # unconditionally, exactly like data_assembly's briefing consumer
    # (data_assembly.py) and the API path. Skipping it would let gated
    # lines such as "ETH Skew 25D" ship until the render scrub deleted the
    # whole line, legitimate extremes included. Stripping BEFORE the
    # top-5 cut lets permitted entries fill the freed slots.
    from carrac.compliance.deribit_gate import strip_gated_anomalies
    anomalies = strip_gated_anomalies(anomalies)
    if not anomalies:
        return ""

    # Sort by |zscore| desc, cap at 5
    top5 = sorted(
        anomalies,
        key=lambda a: abs(a.get('zscore') or 0),
        reverse=True,
    )[:5]

    from quant.anomaly_filter import format_anomaly_date_suffix

    items: list[str] = []
    for a in top5:
        metric = a.get('metric', '')
        asset = a.get('asset', '')
        zscore = a.get('zscore') or 0
        pct = a.get('percentile')
        label = _prettify_metric(metric, asset)
        # This header says "Active" — without a date a reader has
        # no way to tell a today reading from a JAPAN_10Y read three weeks
        # stale. Always carry the date; call out staleness explicitly past 7d.
        date_suffix = format_anomaly_date_suffix(a)
        if pct is not None:
            items.append(f"{label} {zscore:+.1f}σ (p{pct:.0f}){date_suffix}")
        else:
            items.append(f"{label} {zscore:+.1f}σ{date_suffix}")

    return "## Active Extremes (|z| ≥ 2)\n" + " · ".join(items)


# ──────────────────────────────────────────────────────────────────────────────
# G1b — Market Pulse one-liner (morning general report)
# ──────────────────────────────────────────────────────────────────────────────

def build_sentiment_vol_block(api: Optional[Callable] = None) -> str:
    """One-line Market Pulse: Fear&Greed · BTC L/S · MVRV-z · MOVE. Deterministic, facts-only."""
    _api = api if api is not None else _default_api()

    segments: list[str] = []

    # ── /api/market-sentiment: Fear&Greed, BTC L/S, MVRV-z ──────────────────
    try:
        data = _api('/api/market-sentiment')
        sentiment = (data or {}).get('sentiment', {}) or {}

        fg = sentiment.get('fear_greed') or {}
        fg_value = fg.get('value')
        fg_class = fg.get('classification')
        if fg_value is not None and fg_class:
            segments.append(f"Fear & Greed {fg_value} ({fg_class})")

        ls = sentiment.get('long_short') or {}
        btc_ls = ls.get('BTC') or {}
        btc_ratio = btc_ls.get('ratio')
        btc_short_pct = btc_ls.get('shortPct')
        if btc_ratio is not None and btc_short_pct is not None:
            segments.append(f"BTC L/S {btc_ratio:.2f} ({btc_short_pct:.0f}% short)")

        mvrv = sentiment.get('mvrv_zscore') or {}
        mvrv_z = mvrv.get('zscore')
        mvrv_zone = mvrv.get('zone')
        if mvrv_z is not None and mvrv_zone:
            segments.append(f"MVRV-z {mvrv_z:+.2f} ({mvrv_zone})")

    except Exception as e:
        log.warning("build_sentiment_vol_block: market-sentiment call failed: %s", e)

    # ── /api/quant/snapshot: MOVE bond-vol ───────────────────────────────────
    try:
        snap = _api('/api/quant/snapshot')
        bond_vol = ((snap or {}).get('bond_vol') or {})
        move_level = bond_vol.get('level')
        move_label = bond_vol.get('label')
        if move_level is not None and move_label:
            segments.append(f"MOVE {move_level:.0f} ({move_label})")
    except Exception as e:
        log.warning("build_sentiment_vol_block: quant-snapshot call failed: %s", e)

    if not segments:
        return ""

    return "## Market Pulse\n" + " · ".join(segments)


# ──────────────────────────────────────────────────────────────────────────────
# G1c — Cross-Asset Heat strip (morning general report)
# ──────────────────────────────────────────────────────────────────────────────

# Ordered groups: (label, [symbols])
_HEAT_GROUPS: list[tuple[str, list[str]]] = [
    ("Crypto",    ["BTC", "ETH", "SOL"]),
    ("Equity",    ["SPX", "NDX"]),
    ("Commodity", ["GOLD", "OIL"]),
    ("Macro",     ["DXY", "TLT", "HYG"]),
]


def build_cross_asset_heat_block(
    symbols: list[str] | None = None,
    get_facts=None,
) -> str:
    """Render cross-asset price-percentile heat strip as markdown.

    Deterministic, facts-only, non-advice.  Reads ``range_pctile`` from each
    asset's cached facts bundle; skips assets whose bundle is missing or has
    no ``range_pctile`` key.  Omits group lines entirely when none of their
    assets resolved.  Returns ``""`` when no asset yields a value.

    Parameters
    ----------
    symbols   : ordered list of symbols to look up.  ``None`` → use the
                canonical 10-asset ordered set from ``_HEAT_GROUPS``.
    get_facts : callable(symbol) → bundle dict | None.  Defaults to
                ``carrac.facts.wire.get_asset_facts`` (lazy import).
    """
    if get_facts is None:
        from carrac.facts.wire import get_asset_facts
        get_facts = get_asset_facts

    # Determine which symbols to query
    if symbols is not None:
        lookup_syms: list[str] = symbols
    else:
        lookup_syms = [s for _, syms in _HEAT_GROUPS for s in syms]

    # Build pct lookup
    pct_by_symbol: dict[str, int] = {}
    for sym in lookup_syms:
        try:
            bundle = get_facts(sym)
            if bundle and isinstance(bundle, dict):
                rp = bundle.get("range_pctile")
                if rp and isinstance(rp, dict) and rp.get("pct") is not None:
                    pct_by_symbol[sym] = int(rp["pct"])
        except Exception:
            pass

    if not pct_by_symbol:
        return ""

    lines = ["## Cross-Asset Heat", ""]
    for group_name, group_syms in _HEAT_GROUPS:
        parts: list[str] = []
        for sym in group_syms:
            if sym in pct_by_symbol:
                parts.append(f"{sym} p{pct_by_symbol[sym]:02d}")
        if parts:
            header = f"{group_name + ':':<12}"
            lines.append(header + " · ".join(parts))

    lines.append("")
    lines.append("_Where each asset sits in its 90-day range (price percentile). Not advice._")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# G1d — Correlation-Regime Shift block (morning general report)
# ──────────────────────────────────────────────────────────────────────────────

def build_correlation_shift_block(fetch=None) -> str:
    """Render week-over-week correlation shifts as markdown.

    Deterministic, facts-only, non-advice.  Reads from quant.correlations via
    carrac.facts.correlations.get_correlation_shifts (lazy default).

    Parameters
    ----------
    fetch : callable() -> list[dict]  — defaults to get_correlation_shifts.
            Each dict: {a, b, prior, today, shift, became_significant}.

    Returns
    -------
    Markdown block string, or ``""`` when there are no notable shifts.
    """
    if fetch is None:
        from carrac.facts.correlations import get_correlation_shifts  # noqa: PLC0415
        fetch = get_correlation_shifts

    try:
        shifts = fetch()
    except Exception as exc:
        log.warning("build_correlation_shift_block: fetch failed: %s", exc)
        return ""

    if not shifts:
        return ""

    lines = ["## Correlation Shifts (60d, WoW)", ""]
    for s in shifts:
        a_label = _prettify_metric(s["a"], "")
        b_label = _prettify_metric(s["b"], "")
        prior = s["prior"]
        today = s["today"]
        shift = s["shift"]
        sign = "+" if shift >= 0 else "−"
        abs_shift = abs(shift)
        # Format: prior value (may be negative)
        prior_str = f"{prior:.2f}"
        today_str = f"{today:.2f}"
        shift_str = f"{sign}{abs_shift:.2f}"
        line = f"{a_label}–{b_label}  {prior_str} → {today_str}  ({shift_str})"
        if s.get("became_significant"):
            line += ", now significant"
        lines.append(line)

    lines.append("")
    lines.append("_Week-over-week change in 60-day Spearman correlation. Not advice._")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# G1c — Whale Watch (Hyperliquid smart-money positioning, morning general report)
# ──────────────────────────────────────────────────────────────────────────────

_BIAS_THRESHOLDS = (
    (65.0, "net long"),
    (55.0, "mildly net long"),
    (45.0, "balanced"),
    (35.0, "mildly net short"),
)


def _bias_label(value: float) -> str:
    """Map a SMART_MONEY_BIAS value (0–100) to a human label."""
    for threshold, label in _BIAS_THRESHOLDS:
        if value >= threshold:
            return label
    return "net short"


def build_smart_money_block(assets: Optional[list] = None, fetch=None) -> str:
    """Render a Whale Watch block from quant.onchain SMART_MONEY_BIAS.

    Shows the net long/short bias of profitable Hyperliquid traders for the
    available crypto assets (or the supplied ``assets`` list when provided).
    Returns "" when no data is available or the query fails.

    Non-advice: shows observed on-chain positioning statistics only. Never
    uses buy/sell/should/recommend language.

    Parameters
    ----------
    assets : optional list of asset symbols to filter (e.g. ['BTC', 'ETH']).
             When None, returns all available assets from the DB.
    fetch  : callable() -> list[dict] with keys {asset, value, ts}.
             Defaults to a live quant.onchain query. Inject in tests.
    """
    if fetch is not None:
        try:
            rows = fetch()
        except Exception as exc:
            log.warning("build_smart_money_block: fetch failed: %s", exc)
            return ""
    else:
        try:
            from carrac.db_pool import get_quant_conn as get_conn  # noqa: PLC0415
            with get_conn() as conn:
                cur = conn.cursor()
                if assets:
                    cur.execute(
                        "SELECT asset, value, ts FROM quant.onchain "
                        "WHERE metric = 'SMART_MONEY_BIAS' "
                        "AND ts = (SELECT MAX(ts) FROM quant.onchain "
                        "          WHERE metric = 'SMART_MONEY_BIAS') "
                        "AND asset = ANY(%s) "
                        "ORDER BY asset",
                        (list(assets),),
                    )
                else:
                    cur.execute(
                        "SELECT asset, value, ts FROM quant.onchain "
                        "WHERE metric = 'SMART_MONEY_BIAS' "
                        "AND ts = (SELECT MAX(ts) FROM quant.onchain "
                        "          WHERE metric = 'SMART_MONEY_BIAS') "
                        "ORDER BY asset"
                    )
                rows = cur.fetchall()
        except Exception as exc:
            log.warning("build_smart_money_block: DB query failed: %s", exc)
            return ""

    if not rows:
        return ""

    data_date = str(rows[0]["ts"]) if rows[0].get("ts") else ""
    heading = f"## Whale Watch — Hyperliquid Smart-Money Positioning ({data_date})"

    items: list[str] = []
    for row in rows:
        asset = row["asset"]
        value = float(row["value"])
        items.append(f"{asset} {value:.0f} ({_bias_label(value)})")

    body = " · ".join(items)
    note = (
        "Scale: 0=all short, 100=all long, 50=balanced. "
        "Positioning of profitable Hyperliquid traders — "
        "observed data to evaluate, not advice."
    )
    return "\n".join([heading, body, note])


# ──────────────────────────────────────────────────────────────────────────────
# G1e — Bitcoin Network Fundamentals (morning general report)
# ──────────────────────────────────────────────────────────────────────────────

def _humanize_usd(value: float) -> str:
    """Format a USD value with B/M/K suffix, e.g. 48_300_000 → '$48.3M'."""
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    if value >= 1e3:
        return f"${value / 1e3:.1f}K"
    return f"${value:.0f}"


def _btc_supply_from_height(height: int) -> tuple[float, float, int]:
    """Deterministic BTC circulating supply from block height.

    Halving schedule: 210,000-block epochs; block-subsidy halves each epoch
    starting at 50 BTC/block (epoch 0).  All arithmetic is in BTC (float).

    Returns
    -------
    (circulating_supply_btc, current_epoch_subsidy_btc, blocks_to_next_halving)
    """
    _HALVING_INTERVAL = 210_000
    _GENESIS_SUBSIDY = 50.0  # BTC/block, epoch 0

    epoch = height // _HALVING_INTERVAL
    subsidy = _GENESIS_SUBSIDY / (2 ** epoch)

    # Sum all fully-completed epochs
    supply = sum(
        (_GENESIS_SUBSIDY / (2 ** e)) * _HALVING_INTERVAL
        for e in range(epoch)
    )
    # Add the partial current epoch (inclusive of `height`)
    blocks_in_epoch = height - epoch * _HALVING_INTERVAL + 1
    supply += subsidy * blocks_in_epoch

    next_halving = (epoch + 1) * _HALVING_INTERVAL
    return supply, subsidy, next_halving - height


def build_btc_network_block(fetch=None) -> str:
    """Render a Bitcoin Network Fundamentals panel from quant.onchain (asset='BTC').

    Reads the latest available row for each metric independently so a partial
    collector run (not all metrics present) still renders what is available.
    Returns "" when no rows are present at all.

    Non-advice: displays observed on-chain network statistics only.  Never uses
    buy/sell/should/recommend language.

    Parameters
    ----------
    fetch : callable() -> list[dict]  — each dict has keys {metric, value, ts}.
            Defaults to a live quant.onchain query.  Inject in tests.
    """
    if fetch is not None:
        try:
            rows = fetch()
        except Exception as exc:
            log.warning("build_btc_network_block: fetch failed: %s", exc)
            return ""
    else:
        try:
            from carrac.db_pool import get_quant_conn as get_conn  # noqa: PLC0415
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT DISTINCT ON (metric) metric, value, ts "
                    "FROM quant.onchain "
                    "WHERE asset = 'BTC' "
                    "  AND metric IN ("
                    "    'BTC_MINER_REVENUE_USD', 'BTC_HASHRATE', 'BTC_TX_FEES_USD',"
                    "    'BTC_MEMPOOL_VBYTES', 'BTC_DIFFICULTY_ADJ', 'BTC_BLOCK_HEIGHT'"
                    "  ) "
                    "ORDER BY metric, ts DESC"
                )
                rows = cur.fetchall()
        except Exception as exc:
            log.warning("build_btc_network_block: DB query failed: %s", exc)
            return ""

    if not rows:
        return ""

    # Index rows by metric for O(1) lookup; keep most-recent ts for the heading
    data: dict[str, dict] = {r["metric"]: r for r in rows}
    dates = [str(r["ts"]) for r in rows if r.get("ts")]
    data_date = max(dates) if dates else ""

    lines: list[str] = [f"## Bitcoin Network Fundamentals ({data_date})"]

    # Hashrate (EH/s)
    if "BTC_HASHRATE" in data:
        hr = float(data["BTC_HASHRATE"]["value"])
        lines.append(f"Hashrate: {hr:.1f} EH/s")

    # Miner revenue (USD/day)
    if "BTC_MINER_REVENUE_USD" in data:
        rev = float(data["BTC_MINER_REVENUE_USD"]["value"])
        lines.append(f"Miner Revenue: {_humanize_usd(rev)}/day")

    # TX fees (USD/day)
    if "BTC_TX_FEES_USD" in data:
        fees = float(data["BTC_TX_FEES_USD"]["value"])
        lines.append(f"TX Fees: {_humanize_usd(fees)}/day")

    # Mempool backlog (vbytes → MB)
    if "BTC_MEMPOOL_VBYTES" in data:
        mem_vbytes = float(data["BTC_MEMPOOL_VBYTES"]["value"])
        mem_mb = mem_vbytes / 1_000_000
        lines.append(f"Mempool Backlog: {mem_mb:.1f} MB")

    # Difficulty adjustment (%)
    if "BTC_DIFFICULTY_ADJ" in data:
        diff = float(data["BTC_DIFFICULTY_ADJ"]["value"])
        sign = "+" if diff >= 0 else ""
        lines.append(f"Difficulty Adj: {sign}{diff:.2f}%")

    # Block height + deterministic supply/issuance (display-only)
    if "BTC_BLOCK_HEIGHT" in data:
        height = int(float(data["BTC_BLOCK_HEIGHT"]["value"]))
        supply_btc, subsidy, blocks_to_halving = _btc_supply_from_height(height)
        supply_m = supply_btc / 1_000_000
        lines.append(
            f"Block Height: {height:,} · Supply: {supply_m:.2f}M BTC · "
            f"Issuance: {subsidy:.4f} BTC/block · "
            f"Blocks to next halving: {blocks_to_halving:,}"
        )

    # Guard: if only the heading survived (all metrics absent), skip
    if len(lines) == 1:
        return ""

    lines.append(
        "On-chain network data observed from the Bitcoin blockchain. "
        "Presented as information to evaluate, not advice."
    )
    return "\n".join(lines)
