"""Editorial confluence-ladder engine.

Ported from the retired editorial v3 preview generator.
Consumes a ``get_symbol_confluence`` payload — the scanner's enriched per-asset
zones (price, strength, tier, members) — and produces structured render rows for
the spatial key-levels ladder: resistance above, price now, support below. Each
zone leads with HOW STRONG it is (a bar + tier word + signal count), the
individual reference levels underneath as supporting evidence.

Pure functions: (payload dict) → structured rows. No HTML, no I/O, no network.
A missing/cold payload yields ``None`` so the template renders nothing.
"""

from __future__ import annotations

STRENGTH_FULL = 16.0       # weighted strength that fills the visual bar
_STRONG_PICK_MIN = 5.0     # a farther wall this strong still earns a ladder slot
_PROX_FADE_PCT = 6.0       # proximity weight fades to 0 by this % away

# tier → headline word. The number of signals is the moat; surface its weight.
TIER_WORD = {"strong": "Strong wall", "medium": "Moderate", "faint": "Light"}

# Editorial spelled-out names — recognition beats decoding. Overrides the terser
# scanner LABEL_NAMES carried on each member. Target #2 of EDITORIAL-V3-PORT.
EDITORIAL_LEVEL_NAMES = {
    "PDH": "Previous Day High", "PDL": "Previous Day Low",
    "PWH": "Previous Week High", "PWL": "Previous Week Low",
    "PMH": "Previous Month High", "PML": "Previous Month Low",
    "MH": "Month High", "ML": "Month Low",
    "EMA200": "200-Day Average", "SMA200": "200-Day Average",
    "EMA50": "50-Day Average", "EMA21": "21-Day Average",
    "EMA30": "30-Day Average", "EMA60": "60-Day Average",
    "PIVOT": "Monthly Pivot", "TC": "Pivot Range Top", "BC": "Pivot Range Bottom",
    "S1": "Pivot S1", "S2": "Pivot S2", "S3": "Pivot S3",
    "R1": "Pivot R1", "R2": "Pivot R2", "R3": "Pivot R3",
    "BB_U": "Upper Bollinger Band", "BB_M": "Mid Bollinger Band",
    "BB_L": "Lower Bollinger Band",
    "POC": "Volume POC", "VAH": "Value Area High", "VAL": "Value Area Low",
    "SW_H": "Swing High", "SW_L": "Swing Low",
    "SW_PH": "Previous Swing High", "SW_PL": "Previous Swing Low",
}


def zone_refs(zone: dict) -> list[str]:
    """Editorial member names, heaviest-weight first, deduped.

    The magnets (200-Day Average, Volume POC, Monthly Pivot) lead; light levels
    (Previous Day High) trail. Names use the editorial override, falling back to
    the member's own scanner name, then its code.
    """
    members = zone.get("members") or []
    ordered = sorted(members, key=lambda m: -(m.get("weight") or 1.0))
    names: list[str] = []
    for m in ordered:
        code = m.get("code", "")
        name = EDITORIAL_LEVEL_NAMES.get(code) or m.get("name") or code
        if name and name not in names:
            names.append(name)
    return names


def pick_zones(zones: list[dict], price: float) -> list[dict]:
    """Nearest two zones by distance, plus the strongest if it's a strong wall
    not already chosen — so a stacked wall farther out still shows while the
    friction that actually matters today leads. Returned high→low by price for
    the top-down ladder."""
    if not price:
        return []
    usable = [z for z in zones if z.get("price")]
    if not usable:
        return []
    by_dist = sorted(usable, key=lambda z: abs(z["price"] - price))
    chosen = by_dist[:2]
    strongest = max(usable, key=lambda z: z.get("strength") or 0.0)
    if (strongest.get("strength") or 0.0) >= _STRONG_PICK_MIN and strongest not in chosen:
        chosen = chosen + [strongest]
    return sorted(chosen, key=lambda z: z["price"], reverse=True)


def _render_zone(zone: dict, price: float) -> dict:
    """One zone rendered spatially: price + signed distance, a strength bar, the
    tier headline + signal count, then the heaviest reference levels as evidence."""
    strength = zone.get("strength") or 0.0
    tier = zone.get("tier") or "faint"
    count = zone.get("count")
    if count is None:
        count = len(zone.get("members") or [])
    return {
        "price": zone["price"],
        "dist_pct": (zone["price"] - price) / price * 100,
        "strength": strength,
        "tier": tier,
        "tier_word": TIER_WORD.get(tier, tier.title()),
        "signal_count": count,
        "bar_pct": int(max(8, min(100, round(strength / STRENGTH_FULL * 100)))),
        "refs": zone_refs(zone),
        "side": zone.get("side", ""),
    }


def ladder(payload: dict | None) -> dict | None:
    """Build the spatial key-levels ladder from a ``get_symbol_confluence``
    payload: resistance zones above, support zones below. Returns ``None`` when
    there's no usable confluence (cold/absent payload or no price)."""
    if not payload:
        return None
    price = payload.get("price")
    if not price:
        return None
    resistances = [_render_zone(z, price)
                   for z in pick_zones(payload.get("resistances") or [], price)]
    supports = [_render_zone(z, price)
                for z in pick_zones(payload.get("supports") or [], price)]
    return {"price": price, "resistances": resistances, "supports": supports}


def levels_summary(lad: dict | None) -> dict | None:
    """One-line card summary: the NEAREST support + resistance zone (editorial v4).
    Full ladder collapses behind <details>; this line is what stays visible."""
    if not lad:
        return None
    def _side(zones):
        if not zones:
            return None
        z = zones[0]
        return {"price": z.get("price"), "dist_pct": z.get("dist_pct"),
                "tier_word": z.get("tier_word"), "signal_count": z.get("signal_count")}
    sup, res = _side(lad.get("supports")), _side(lad.get("resistances"))
    if sup is None and res is None:
        return None
    return {"support": sup, "resistance": res}


def quiet_wall(payload: dict | None) -> dict | None:
    """The single strongest confluence wall within reach of price — a compact flag
    so a stacked wall right at price (e.g. an 8-level ceiling) isn't hidden when the
    asset is otherwise quiet. Only 'strong'-tier zones, proximity-weighted (fades to
    0 by 6% away); None when nothing strong is within reach. Returns
    ``{side, word, dist_pct, signal_count}``."""
    if not payload:
        return None
    price = payload.get("price")
    if not price:
        return None
    best = None  # (score, side, dist_pct, zone)
    for side, zones in (("res", payload.get("resistances") or []),
                        ("sup", payload.get("supports") or [])):
        for z in zones:
            if z.get("tier") != "strong":
                continue
            zp = z.get("price")
            if not zp:
                continue
            dist = (zp - price) / price * 100
            score = (z.get("strength") or 0.0) * max(0.0, 1 - abs(dist) / _PROX_FADE_PCT)
            if best is None or score > best[0]:
                best = (score, side, dist, z)
    if best is None or best[0] < 1.0:
        return None
    _, side, dist, z = best
    count = z.get("count")
    if count is None:
        count = len(z.get("members") or [])
    return {
        "side": side,
        "word": "ceiling" if side == "res" else "floor",
        "dist_pct": dist,
        "signal_count": count,
    }


def ladder_for(symbol: str) -> dict | None:
    """I/O adapter: read the symbol's enriched confluence from the scanner cache
    and build its ladder. Returns ``None`` when the cache is cold or the symbol
    is absent. The only impure functions here — kept thin so the engine above
    stays testable without the scanner dependency."""
    from carrac.scanner.confluence_query import get_symbol_confluence
    payload, status = get_symbol_confluence(symbol)
    return ladder(payload if status == "ok" else None)


