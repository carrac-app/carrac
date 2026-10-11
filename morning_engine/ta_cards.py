"""Deterministic per-asset TA cards for the free morning report.

Public API:
  render_asset_card(bundle)          -> str  (one markdown card from a bundle)
  build_ta_cards_block(symbols, ...) -> str  (heading + up to 3 cards; "" if none)

Non-advice contract: cards contain ONLY observed facts (price, MA alignment,
levels, streaks, confirmed closes, RSI, volume).  No buy/sell/should/enter/
exit/target/take profit language anywhere.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from carrac.staleness import history_is_dead

log = logging.getLogger(__name__)

# Heading that precedes the block when at least one card is produced.
_CARDS_HEADING = "## Spotlight — Levels & Structure"

# MA-stack state → human-readable description.
_MA_STATE_LABELS: dict[str, str] = {
    "bullish": "price above all MAs (up stack)",
    "bearish": "price below all MAs (down stack)",
    "bullish_partial": "mixed, leaning up",
    "bearish_partial": "mixed, leaning down",
    "insufficient": "insufficient history for the 200-day",
}

# States that have a "bars_held" concept worth appending.
_STACK_STATES = {"bullish", "bearish", "bullish_partial", "bearish_partial"}

# Decisive-close curation thresholds.
_DECISIVE_RECENT_MAX_BARS = 21  # bars_ago must be ≤ this to be shown
_MAX_DECISIVE = 3               # max decisive-close lines per card
_MAX_STREAKS = 3                # max streak lines per card

# Mayer-multiple percentile bands.
def _ordinal(n: int) -> str:
    """Return the ordinal suffix for a number (st, nd, rd, th)."""
    if 10 <= n % 100 <= 20:
        # Special case for 11-13, 111-113, etc.
        return "th"
    elif n % 10 == 1:
        return "st"
    elif n % 10 == 2:
        return "nd"
    elif n % 10 == 3:
        return "rd"
    else:
        return "th"


def _mayer_band(pct: int) -> str:
    if pct <= 20:
        return "(washed out)"
    if pct >= 80:
        return "(stretched)"
    return "(mid-range)"


def _fmt_price(p) -> str:
    """Format price with magnitude-appropriate precision.

    Precision tiers by abs(p):
      >= 1000   →  "$61,240"    (no decimals)
      >= 1      →  "$3.50"      (2 dp)
      >= 0.01   →  "$0.2030"    (4 dp)
      >= 0.0001 →  "$0.000123"  (6 dp)
      else      →  "$0.00001234" (8 dp, covers sub-cent crypto)

    Returns "$—" for None and str(p) for other non-numeric inputs so
    callers never raise.
    """
    try:
        v = float(p)
    except (TypeError, ValueError):
        return "$—" if p is None else str(p)
    if v >= 1000:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    if v >= 0.01:
        return f"${v:.4f}"
    if v >= 0.0001:
        return f"${v:.6f}"
    return f"${v:.8f}"


def _parse_hhmm_utc(as_of: str) -> str:
    """Extract HH:MM UTC from an ISO timestamp string; falls back to raw string."""
    try:
        dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return as_of


def render_asset_card(bundle: dict) -> str:
    """Render a single markdown TA card from a facts bundle dict.

    Gracefully omits any line whose data is missing.  Never includes
    advice language (buy / sell / should / enter / exit / target).
    """
    lines: list[str] = []

    symbol = bundle.get("symbol", "?")
    price_raw = bundle.get("price")
    as_of = bundle.get("as_of", "")

    # --- Header ---
    if price_raw is not None:
        hhmm = _parse_hhmm_utc(as_of) if as_of else ""
        time_part = f" (as of {hhmm} UTC)" if hhmm else ""
        lines.append(f"### {symbol} — {_fmt_price(float(price_raw))}{time_part}")
    else:
        lines.append(f"### {symbol}")

    # --- Trend line (1D): MA stack + RSI + volume + candle pattern ---
    ma_stack = bundle.get("ma_stack") or {}
    state = ma_stack.get("state", "")
    trend_parts: list[str] = []

    if state in _MA_STATE_LABELS:
        label = _MA_STATE_LABELS[state]
        bars_held = ma_stack.get("bars_held")
        if state in _STACK_STATES and bars_held is not None:
            label = f"{label}, {bars_held} sessions"
        cross = ma_stack.get("cross", "none")
        if cross == "golden_cross":
            label += " · golden cross"
        elif cross == "death_cross":
            label += " · death cross"
        trend_parts.append(label)

    # RSI
    rsi_block = bundle.get("rsi") or {}
    rsi_val = rsi_block.get("value")
    rsi_zone = rsi_block.get("zone")
    if rsi_val is not None:
        rsi_str = f"RSI {rsi_val:.0f}"
        if rsi_zone:
            rsi_str += f" ({rsi_zone})"
        trend_parts.append(rsi_str)

    # Volume (only when reliable)
    vol_block = bundle.get("volume") or {}
    if vol_block.get("reliable"):
        rvol = vol_block.get("rvol")
        sig = vol_block.get("signal")
        if rvol is not None:
            vol_str = f"volume {rvol:.1f}× avg"
            if sig:
                vol_str += f" ({sig})"
            trend_parts.append(vol_str)

    # Candle pattern label
    candle_block = bundle.get("candle") or {}
    pattern = candle_block.get("pattern_label")
    if pattern:
        trend_parts.append(f"{pattern.replace('_', ' ')} candle")

    if trend_parts:
        lines.append("**Trend (1D):** " + " · ".join(trend_parts))

    # --- Stretch (Mayer multiple) ---
    stretch = bundle.get("stretch") or {}
    if stretch:
        mayer = stretch.get("mayer")
        mayer_pct = stretch.get("mayer_pct")
        if mayer is not None and mayer_pct is not None:
            band = _mayer_band(int(mayer_pct))
            ordinal = _ordinal(int(mayer_pct))
            lines.append(
                f"**Stretch:** {mayer:.2f}× its 200-week average"
                f" — {mayer_pct}{ordinal} percentile {band}"
            )

    # --- Levels & clusters ---
    levels_block = bundle.get("levels") or {}
    clusters = levels_block.get("clusters") or []
    if clusters and price_raw is not None:
        current_price = float(price_raw)
        resistances = [c for c in clusters if c.get("side") == "resistance"]
        supports = [c for c in clusters if c.get("side") == "support"]

        for c in resistances:
            cp = c.get("price")
            members = c.get("members") or []
            count = c.get("count", len(members))
            dist_pct = c.get("dist_pct")
            if cp is not None:
                m_str = "+".join(members) if members else ""
                dist_str = f" {dist_pct:+.1f}%" if dist_pct is not None else ""
                cluster_str = f" ({m_str}, {count})" if m_str else f" ({count})"
                lines.append(f"▲ resistance {_fmt_price(float(cp))}{cluster_str}{dist_str}")

        if resistances and supports:
            lines.append(f"── price {_fmt_price(current_price)} ──")

        for c in supports:
            cp = c.get("price")
            members = c.get("members") or []
            count = c.get("count", len(members))
            dist_pct = c.get("dist_pct")
            if cp is not None:
                m_str = "+".join(members) if members else ""
                dist_str = f" {dist_pct:+.1f}%" if dist_pct is not None else ""
                cluster_str = f" ({m_str}, {count})" if m_str else f" ({count})"
                lines.append(f"▼ support {_fmt_price(float(cp))}{cluster_str}{dist_str}")

    # --- Level memory (streaks) ---
    streaks = bundle.get("streaks") or []
    # Sort longest-held first, cap at _MAX_STREAKS.
    curated_streaks = sorted(
        streaks, key=lambda s: s.get("bars", 0), reverse=True
    )[:_MAX_STREAKS]
    for s in curated_streaks:
        sp = s.get("price")
        bars = s.get("bars")
        stype = s.get("type", "")
        if sp is not None and bars is not None and stype:
            lines.append(f"**Level memory:** {stype} {_fmt_price(float(sp))} for {bars} sessions")

    # --- Confirmed decisive closes ---
    decisive = bundle.get("decisive_close") or []
    # Filter to recent only, sort most-recent-first (ascending bars_ago), cap at _MAX_DECISIVE.
    recent_decisive = [
        d for d in decisive
        if d.get("bars_ago") is not None and d.get("bars_ago") <= _DECISIVE_RECENT_MAX_BARS
    ]
    recent_decisive.sort(key=lambda d: d.get("bars_ago", 0))
    recent_decisive = recent_decisive[:_MAX_DECISIVE]
    for d in recent_decisive:
        direction = d.get("direction", "")
        level_name = d.get("level", "")
        bars_ago = d.get("bars_ago")
        if direction and level_name and bars_ago is not None:
            lines.append(f"**Confirmed:** closed {direction} {level_name} ({bars_ago} sessions ago)")

    # --- Monthly CPR ---
    monthly_cpr = levels_block.get("monthly_cpr") or {}
    cpr_pos = monthly_cpr.get("position")
    if cpr_pos:
        lines.append(f"**Monthly CPR:** {cpr_pos.replace('_', ' ')}")

    # --- Funding streak (crypto-only; omitted when key absent) ---
    funding = bundle.get("funding")
    if funding:
        try:
            f_sign = funding.get("sign", "")
            f_days = funding.get("streak_days")
            f_pct = funding.get("percentile")
            if f_sign and f_days is not None and f_pct is not None:
                ordinal = _ordinal(int(f_pct))
                lines.append(
                    f"**Funding:** {f_sign} {f_days} sessions · {f_pct}{ordinal} pct"
                )
        except Exception:  # noqa: BLE001
            pass

    # --- Weekly summary ---
    weekly = bundle.get("weekly") or {}
    if weekly:
        w_ma = weekly.get("ma_stack") or {}
        w_state = w_ma.get("state", "")
        w_label = _MA_STATE_LABELS.get(w_state, "")
        w_parts: list[str] = []
        if w_label:
            w_parts.append(w_label)
        # Add first weekly streak if present
        w_streaks = weekly.get("streaks") or []
        if w_streaks:
            ws = w_streaks[0]
            wsp = ws.get("price")
            wsb = ws.get("bars")
            wst = ws.get("type", "")
            if wsp is not None and wsb is not None and wst:
                w_parts.append(f"{wst} {_fmt_price(float(wsp))} for {wsb} sessions")
        if w_parts:
            lines.append("**Weekly:** " + "; ".join(w_parts))

    return "\n".join(lines)


def build_ta_cards_block(
    symbols: list[str],
    get_facts=None,
) -> str:
    """Build heading + up to 3 TA cards for the given symbols.

    Args:
        symbols:   Ordered list of symbols (first 3 with a cached bundle are used).
        get_facts: Callable(symbol) -> dict|None. Defaults to
                   carrac.facts.wire.get_asset_facts. Injectable for tests.

    Returns:
        Markdown string with heading and cards, or "" when no bundles are found.
    """
    if get_facts is None:
        from carrac.facts.wire import get_asset_facts
        get_facts = get_asset_facts

    cards: list[str] = []
    for sym in symbols:
        if len(cards) >= 3:
            break
        try:
            bundle = get_facts(sym)
        except Exception as exc:  # noqa: BLE001
            log.warning("ta_cards: get_facts(%s) failed: %s", sym, exc)
            bundle = None
        if not bundle:
            continue
        # A dead-history bundle carries no facts, so its card
        # renders as a bare "### MKR" heading and, worse, occupies one of only
        # three spotlight slots that a live asset should have had.
        if history_is_dead(bundle.get('data_stale')):
            log.info("ta_cards: skipping %s — %s", sym,
                     (bundle['data_stale'] or {}).get('note', 'history is stale'))
            continue
        try:
            card = render_asset_card(bundle)
            cards.append(card)
        except Exception as exc:  # noqa: BLE001
            log.warning("ta_cards: render_asset_card(%s) failed: %s", sym, exc)

    if not cards:
        return ""

    return _CARDS_HEADING + "\n\n" + "\n\n".join(cards)
