"""
Playbook analytics — per-setup / per-archetype expectancy across the trade
journal.

Every metric here is grounded in columns that already exist on
``trade_journal`` (see ``carrac/content_db.py``): ``entry_price``,
``stop_price``, ``exit_price``, ``direction``, ``asset``, ``setup_type``,
``tags``, ``opened_at``, ``closed_at``, ``mfe``, ``mae``. No schema change
or migration is needed:

  - R-multiple and percent-return are computed HERE as small, independently
    unit-tested pure functions rather than trusting the persisted
    ``rr_actual`` column. This keeps the analytics testable in isolation
    and decoupled from any future change to how ``rr_actual`` is derived.
    The formula matches ``carrac.content_db.log_trade_close``
    (``abs(risk)`` so a mis-recorded stop can't silently flip the sign).
  - MFE/MAE are already computed once per closed trade and persisted by
    ``carrac/journal/mfe_mae.py`` (``calculate_and_store_mfe_mae``),
    called from both the in-app close route (``carrac/journal/routes.py``)
    and the ``trade_close`` MCP tool. This module only *reads* the stored
    ``mfe``/``mae`` R-multiples; it does not recompute them.

Grouping dimensions (spec): archetype tag(s), setup, symbol, side,
hour-of-day, day-of-week.

Assumption/limitation (documented, not silently assumed): there is no
``play_id`` foreign key from ``trade_journal`` to the ``plays`` table
today — a journal entry cannot be structurally linked to a specific Play.
The only existing traceability mechanism is the free-text ``tags`` JSON
array, already documented at ``trade_log()`` as "the primary traceability
hook for automation" (e.g. ``tags=["src:agent", "strat:bb_breakdown"]``).
The ``by_tag`` grouping below IS the "linked play / archetype" dimension
here; a dedicated ``play_id`` FK is out of scope (tracked
separately as Play Intelligence).

Wording note: this module returns numbers only. Any user-facing copy
built on top of it (journal.html / journal-page.js, the MCP tool
docstring) must stay strictly descriptive — no "you should", no
edge/recommendation claims (non-advice posture).
"""

import json
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

# R-distribution bucket edges per spec: <=-2R, -2..-1, -1..0, 0..1, 1..2, 2..3, >3R
# Partition is (-inf,-2] (-2,-1] (-1,0] (0,1] (1,2] (2,3] (3,+inf) — no gaps, no overlap.
_R_BUCKET_EDGES = [-2.0, -1.0, 0.0, 1.0, 2.0, 3.0]
R_BUCKET_LABELS = ['<=-2R', '-2..-1', '-1..0', '0..1', '1..2', '2..3', '>3R']

# Groups with fewer closed trades than this get a small-sample caution flag.
SMALL_SAMPLE_N = 20

DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

# Default lookback / row-cap, mirrors ContentDatabase.get_journal_analytics.
DEFAULT_DAYS = 90
DEFAULT_LIMIT = 500


# ─────────────────────────── R / pct primitives ────────────────────────────

def compute_r_multiple(entry_price, exit_price, stop_price, direction) -> Optional[float]:
    """R-multiple = signed PnL / initial risk, or None if unavailable.

    R is UNAVAILABLE when there is no recorded stop (or stop == entry,
    i.e. zero risk) — callers must fall back to :func:`compute_pct_return`
    for such trades rather than inventing a number.
    """
    if entry_price is None or exit_price is None or stop_price is None:
        return None
    risk = abs(entry_price - stop_price)
    if risk == 0:
        return None
    if (direction or 'long').lower() == 'short':
        pnl = entry_price - exit_price
    else:
        pnl = exit_price - entry_price
    return pnl / risk


def compute_pct_return(entry_price, exit_price, direction) -> Optional[float]:
    """Percent return (as a fraction, e.g. 0.05 = +5%), signed by side.

    Fallback metric used for a whole group when any trade in it lacks a
    recorded stop (R unavailable) — see :func:`_group_basis`.
    """
    if not entry_price or exit_price is None:
        return None
    if (direction or 'long').lower() == 'short':
        pnl = entry_price - exit_price
    else:
        pnl = exit_price - entry_price
    return pnl / entry_price


def bucket_label(value: Optional[float]) -> Optional[str]:
    """Classify a metric value (R or pct fraction) into one of 7 buckets."""
    if value is None:
        return None
    for edge, label in zip(_R_BUCKET_EDGES, R_BUCKET_LABELS):
        if value <= edge:
            return label
    return R_BUCKET_LABELS[-1]


# ───────────────────────────── group summary ───────────────────────────────

def _group_basis(trades: List[Dict]) -> str:
    """'r' if every trade in the group has a usable stop, else 'pct'.

    Never mixes R and percent numbers within one group (spec requirement):
    a single trade lacking a stop drops the WHOLE group to the percent
    basis so every number reported for that group is on one consistent
    scale.
    """
    for t in trades:
        entry = t.get('entry_price')
        stop = t.get('stop_price')
        if stop is None or entry is None or entry == stop:
            return 'pct'
    return 'r'


def _metric_value(t: Dict, basis: str) -> Optional[float]:
    if basis == 'r':
        return compute_r_multiple(t.get('entry_price'), t.get('exit_price'),
                                   t.get('stop_price'), t.get('direction'))
    return compute_pct_return(t.get('entry_price'), t.get('exit_price'), t.get('direction'))


def summarize_group(trades: List[Dict]) -> Optional[Dict]:
    """Build the metrics dict for one group of CLOSED trades.

    ``n`` is always populated (spec: "n (ALWAYS displayed)"), even when no
    trade in the group yields a usable metric value.
    """
    n = len(trades)
    if n == 0:
        return None

    basis = _group_basis(trades)
    values = [v for v in (_metric_value(t, basis) for t in trades) if v is not None]
    scored_n = len(values)

    # Win definition: strictly positive metric (breakeven counts as non-win),
    # matching the strict `> 0` split used at the top level of
    # ContentDatabase.get_journal_analytics (as opposed to that module's
    # `_group_stats` helper, which is `>= 0` — the two conventions already
    # coexist in this codebase; this module picks the strict one and is
    # internally consistent).
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]

    win_rate = round(len(wins) / scored_n * 100, 1) if scored_n else None
    expectancy = round(sum(values) / scored_n, 4) if scored_n else None

    gross_win = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    avg_win = round(sum(wins) / len(wins), 4) if wins else None
    avg_loss = round(abs(sum(losses)) / len(losses), 4) if losses else None

    dist_counts = {label: 0 for label in R_BUCKET_LABELS}
    for v in values:
        dist_counts[bucket_label(v)] += 1
    distribution = [{'bucket': label, 'count': dist_counts[label]} for label in R_BUCKET_LABELS]

    mfe_vals = [t['mfe'] for t in trades if t.get('mfe') is not None]
    mae_vals = [t['mae'] for t in trades if t.get('mae') is not None]

    return {
        'n': n,
        'basis': basis,               # 'r' | 'pct' — never mixed within a group
        'scored_n': scored_n,          # trades that actually yielded a metric value
        'win_rate': win_rate,
        'expectancy': expectancy,      # mean R, or mean pct-fraction when basis == 'pct'
        'profit_factor': profit_factor,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'avg_mfe': round(sum(mfe_vals) / len(mfe_vals), 2) if mfe_vals else None,
        'avg_mfe_n': len(mfe_vals),
        'avg_mae': round(sum(mae_vals) / len(mae_vals), 2) if mae_vals else None,
        'avg_mae_n': len(mae_vals),
        'r_distribution': distribution,
        'small_sample': n < SMALL_SAMPLE_N,
    }


# ───────────────────────────── dimension keys ──────────────────────────────

def _extract_tags(t: Dict) -> List[str]:
    """Parse the ``tags`` JSON-array column (stored as a TEXT string on
    both Postgres and SQLite; may already be a list if a caller pre-parsed
    it). Malformed/missing tags yield an empty list rather than raising."""
    raw = t.get('tags')
    if not raw:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, str)]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, str)]
    return []


def _parse_ts(ts) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(str(ts).replace('Z', '').split('+')[0])
    except (TypeError, ValueError):
        return None


def _hour_of_day_label(t: Dict) -> Optional[str]:
    dt = _parse_ts(t.get('opened_at'))
    return f'{dt.hour:02d}:00' if dt else None


def _day_of_week_label(t: Dict) -> Optional[str]:
    dt = _parse_ts(t.get('opened_at'))
    return DAY_NAMES[dt.weekday()] if dt else None


def _finalize(groups: Dict[str, List[Dict]], order: Optional[List[str]] = None) -> Dict[str, Dict]:
    out = {k: summarize_group(v) for k, v in groups.items() if v}
    if not order:
        return out
    ordered = {k: out[k] for k in order if k in out}
    # Anything outside the known order (shouldn't normally happen) still surfaces.
    ordered.update({k: v for k, v in out.items() if k not in ordered})
    return ordered


# ─────────────────────────────── public API ────────────────────────────────

def build_playbook_analytics(trades: List[Dict]) -> Dict:
    """Group CLOSED trades by setup / archetype tag / symbol / side / time,
    and summarize each group.

    ``trades`` is the raw list of dict rows as returned by
    ``ContentDatabase.get_trade_history`` (or any equivalent — this
    function has no DB dependency, which is what makes it unit-testable
    with synthetic trade dicts).
    """
    closed = [
        t for t in trades
        if t.get('closed_at') and t.get('exit_price') is not None and t.get('entry_price') is not None
    ]

    by_setup: Dict[str, List[Dict]] = defaultdict(list)
    by_tag: Dict[str, List[Dict]] = defaultdict(list)
    by_symbol: Dict[str, List[Dict]] = defaultdict(list)
    by_side: Dict[str, List[Dict]] = defaultdict(list)
    by_hour: Dict[str, List[Dict]] = defaultdict(list)
    by_weekday: Dict[str, List[Dict]] = defaultdict(list)

    for t in closed:
        by_setup[t.get('setup_type') or 'untagged'].append(t)
        for tag in _extract_tags(t):
            by_tag[tag].append(t)
        by_symbol[(t.get('asset') or 'unknown').upper()].append(t)
        by_side[(t.get('direction') or 'unknown').lower()].append(t)

        hour_key = _hour_of_day_label(t)
        if hour_key:
            by_hour[hour_key].append(t)
        day_key = _day_of_week_label(t)
        if day_key:
            by_weekday[day_key].append(t)

    hour_order = [f'{h:02d}:00' for h in range(24)]

    return {
        'total_closed_trades': len(closed),
        'by_setup': _finalize(by_setup),
        'by_tag': _finalize(by_tag),
        'by_symbol': _finalize(by_symbol),
        'by_side': _finalize(by_side),
        'by_hour_of_day': _finalize(by_hour, order=hour_order),
        'by_day_of_week': _finalize(by_weekday, order=DAY_NAMES),
    }


def get_playbook_analytics(db, user_id: int, days: int = DEFAULT_DAYS,
                            limit: int = DEFAULT_LIMIT) -> Dict:
    """Entry point used by the Flask route and the MCP tool.

    Fetches closed trades via the existing ``get_trade_history`` (same
    source as ``get_journal_analytics``) and builds the grouped breakdown.
    Tier gating is the CALLER's responsibility (see
    ``carrac/journal/routes.py`` and ``carrac/mcp/tools/journal.py``) —
    this function always computes and returns data.
    """
    trades = db.get_trade_history(user_id, days=days, limit=limit)
    result = build_playbook_analytics(trades)
    result['period_days'] = days
    return result
