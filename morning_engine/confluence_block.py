"""Deterministic 'Levels in Focus' block for the report briefing.

Mirrors calendar_block: code renders the in-focus assets' confluence levels so
the report LLM reproduces them verbatim instead of inventing levels.
"""
from typing import Any, Callable

CONFLUENCE_FOCUS_MAX = 3


def _nearest(payload: dict):
    """Extract nearest support and resistance from payload, compute proximity."""
    price = payload.get("price") or 0
    if not price:
        return None, None, 1.0
    supports = payload.get("supports") or []
    resistances = payload.get("resistances") or []
    below = [s for s in supports if (s.get("price") or 0) and s["price"] < price]
    above = [r for r in resistances if (r.get("price") or 0) and r["price"] > price]
    sup = max(below, key=lambda s: s["price"]) if below else None
    res = min(above, key=lambda r: r["price"]) if above else None
    dists = []
    if sup:
        dists.append(abs(price - sup["price"]) / price)
    if res:
        dists.append(abs(res["price"] - price) / price)
    return sup, res, (min(dists) if dists else 1.0)


def _all_grounded_levels(payload: dict):
    """Every level price the confluence engine actually computed for this asset.

    Each zone carries both its clustered ``price`` and the individual ``members``
    it was built from — CPR pivots (S1/S2/S3, R1/R2/R3, BC/TC), Bollinger bands,
    EMAs, swing points, volume-profile and key levels. Prose inside the
    Levels-in-Focus section legitimately cites these ("the monthly S1 at $3,810",
    "the CPR R1 of $6.59"), so the fabrication scanner must ground on all of
    them, not just the two nearest zones the table renders.
    """
    out = []
    zones = payload.get("all") or (
        (payload.get("supports") or []) + (payload.get("resistances") or []))
    for z in zones:
        if not isinstance(z, dict):
            continue
        if z.get("price"):
            out.append(float(z["price"]))
        for m in z.get("members") or []:
            if isinstance(m, dict) and m.get("price"):
                out.append(float(m["price"]))
    return out


def select_confluence_focus(spotlight_symbols, fetch_fn: Callable[[str], Any],
                            max_assets: int = CONFLUENCE_FOCUS_MAX):
    """Select top-N in-focus assets by proximity to nearest levels.

    Returns list of dicts: {asset, price, support, resistance, proximity, levels}.
    ``levels`` is the full grounded set for the fabrication scanner; the rendered
    table still shows only the nearest support/resistance.
    Abstains (cite-or-abstain) on cold/not_found statuses.
    """
    focus = []
    for sym in spotlight_symbols or []:
        try:
            payload, status = fetch_fn(sym)
        except Exception:
            continue
        if status != "ok" or not payload:
            continue
        sup, res, proximity = _nearest(payload)
        if not sup and not res:
            continue
        focus.append({"asset": sym, "price": payload.get("price") or 0,
                      "support": sup, "resistance": res, "proximity": proximity,
                      "levels": _all_grounded_levels(payload)})
    focus.sort(key=lambda f: f["proximity"])
    return focus[:max_assets]


def _fmt_zone(z, price, side):
    """Format a single support/resistance zone with distance and label."""
    if not z or not z.get("price"):
        return "—"
    dist = abs(z["price"] - price) / price * 100 if price else 0
    arrow = "below" if side == "support" else "above"
    label = z.get("tier") or z.get("strength")
    suffix = f", {label}" if label else ""
    return f"{z['price']:,.2f} ({dist:.1f}% {arrow}{suffix})"


def render_confluence_focus(focus_list):
    """Render deterministic Levels in Focus block as markdown table.

    Returns empty string if focus_list is empty.
    Table includes: Asset, Price, Nearest support, Nearest resistance.
    """
    if not focus_list:
        return ""
    rows = ["## Levels in Focus — REPRODUCE THESE LEVELS VERBATIM",
            "| Asset | Price | Nearest support | Nearest resistance |",
            "|-------|-------|-----------------|--------------------|"]
    for f in focus_list:
        rows.append(
            f"| {f['asset']} | {f['price']:,.2f} | "
            f"{_fmt_zone(f['support'], f['price'], 'support')} | "
            f"{_fmt_zone(f['resistance'], f['price'], 'resistance')} |")
    return "\n".join(rows)
