"""Editorial hierarchy layer for the facts-thick report.

Builds the one-sentence ledes (cross-asset comparative, peer-cluster and the
spotlight chain that picks between them).  Pure functions — no I/O, no clock.
"""
from __future__ import annotations

from typing import Optional

from carrac.facts.labels import asset_name, ordinal
from carrac.facts.sentences import asset_lede


# ─────────────────────────────── 1. comparative_lede ────────────────────────

def comparative_lede(symbol: str, bundles: dict[str, dict]) -> Optional[str]:
    """Return a single comparative sentence if *symbol* stands out among peers.

    Answers "why THIS asset, not another" by ranking *symbol* against the other
    assets in *bundles* across eight dimensions.  Returns the sentence for the
    single highest-priority dimension where *symbol* is the extreme **and** the
    value is actually notable; returns None if *symbol* is not distinctive.

    Priority (highest → lowest):
      1. extreme_funding   — most-negative funding AND depressed price
      2. most_stretched_low  — lowest mayer_pct AND ≤ 10
      3. most_stretched_high — highest mayer_pct AND ≥ 90
      4. lowest_range      — lowest range_pctile AND ≤ 10
      5. highest_range     — highest range_pctile AND ≥ 90
      6. longest_trend     — most bars_held AND ≥ 10
      7. most_oversold     — lowest RSI value with zone=oversold
      8. most_overbought   — highest RSI value with zone=overbought

    Ties: *symbol* counts as the extreme when its value equals the overall
    min/max (tied-for-min with no strictly-lower peer still qualifies).

    Returns None if: *bundles* has < 2 assets, *symbol* is absent, or *symbol*
    is not the standout on any dimension.  Missing fields silently exclude
    that dimension — never raises.
    """
    if len(bundles) < 2 or symbol not in bundles:
        return None

    name = asset_name(symbol)
    own = bundles[symbol]

    def _get(b: dict, *keys):
        """Safely traverse a nested dict; return None if any step is missing."""
        cur = b
        for k in keys:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
            if cur is None:
                return None
        return cur

    # ── priority 1: extreme_funding ──────────────────────────────────────────
    # symbol must have sign=negative and the lowest funding percentile among
    # all negative-sign assets, AND its range_pctile.pct ≤ 25.
    own_f_sign = _get(own, "funding", "sign")
    if own_f_sign == "negative":
        own_fp = _get(own, "funding", "percentile")
        if own_fp is not None:
            neg_fps = [
                _get(b, "funding", "percentile")
                for b in bundles.values()
                if _get(b, "funding", "sign") == "negative"
                and _get(b, "funding", "percentile") is not None
            ]
            own_rp = _get(own, "range_pctile", "pct")
            if neg_fps and own_fp <= min(neg_fps) and own_rp is not None and own_rp <= 25:
                return (
                    f"{name} is the only name pairing a depressed price with "
                    f"deeply negative funding — an unusually rare combination."
                )

    # ── priority 2 & 3: stretch (low then high) ──────────────────────────────
    own_mp = _get(own, "stretch", "mayer_pct")
    if own_mp is not None:
        all_mps = [
            _get(b, "stretch", "mayer_pct")
            for b in bundles.values()
            if _get(b, "stretch", "mayer_pct") is not None
        ]
        if all_mps:
            mayer = _get(own, "stretch", "mayer")
            # priority 2: most_stretched_low
            if own_mp <= min(all_mps) and own_mp <= 10:
                return (
                    f"{name} is the most stretched name on the board — "
                    f"trading at {mayer:.2f}× its 200-week average, "
                    f"the {ordinal(int(own_mp))} percentile of its history."
                )
            # priority 3: most_stretched_high
            if own_mp >= max(all_mps) and own_mp >= 90:
                return (
                    f"{name} is the most extended name on the board — "
                    f"{mayer:.2f}× its 200-week average ({ordinal(int(own_mp))} percentile)."
                )

    # ── priority 4 & 5: range_pctile (low then high) ─────────────────────────
    own_rp = _get(own, "range_pctile", "pct")
    if own_rp is not None:
        all_rps = [
            _get(b, "range_pctile", "pct")
            for b in bundles.values()
            if _get(b, "range_pctile", "pct") is not None
        ]
        if all_rps:
            # priority 4: lowest_range
            if own_rp <= min(all_rps) and own_rp <= 10:
                return (
                    f"{name} is lower in its 90-day range than any other tracked asset "
                    f"— the {ordinal(int(own_rp))} percentile."
                )
            # priority 5: highest_range
            if own_rp >= max(all_rps) and own_rp >= 90:
                return (
                    f"{name} is higher in its 90-day range than any other tracked asset "
                    f"— the {ordinal(int(own_rp))} percentile."
                )

    # ── priority 6: longest_trend ────────────────────────────────────────────
    own_bars = _get(own, "ma_stack", "bars_held")
    if own_bars is not None:
        all_bars = [
            _get(b, "ma_stack", "bars_held")
            for b in bundles.values()
            if _get(b, "ma_stack", "bars_held") is not None
        ]
        if all_bars and own_bars >= max(all_bars) and own_bars >= 10:
            state = _get(own, "ma_stack", "state") or ""
            direction = "bull" if "bull" in state else "bear"
            return (
                f"{name} has held its {direction} stack longer than any other name "
                f"— {own_bars} sessions."
            )

    # ── priority 7: most_oversold ────────────────────────────────────────────
    own_rsi_zone = _get(own, "rsi", "zone")
    own_rsi_val = _get(own, "rsi", "value")
    if own_rsi_zone == "oversold" and own_rsi_val is not None:
        oversold_vals = [
            _get(b, "rsi", "value")
            for b in bundles.values()
            if _get(b, "rsi", "zone") == "oversold"
            and _get(b, "rsi", "value") is not None
        ]
        if oversold_vals and own_rsi_val <= min(oversold_vals):
            return f"{name} is the most oversold name on the board — RSI {own_rsi_val:.0f}."

    # ── priority 8: most_overbought ──────────────────────────────────────────
    if own_rsi_zone == "overbought" and own_rsi_val is not None:
        overbought_vals = [
            _get(b, "rsi", "value")
            for b in bundles.values()
            if _get(b, "rsi", "zone") == "overbought"
            and _get(b, "rsi", "value") is not None
        ]
        if overbought_vals and own_rsi_val >= max(overbought_vals):
            return f"{name} is the most overbought name on the board — RSI {own_rsi_val:.0f}."

    return None


# ─────────────────────────────── 2. cluster_lede ────────────────────────────

# Peer groups for cluster-relative superlatives:  (display word, member symbols).
DEFAULT_CLUSTERS: list[tuple[str, tuple[str, ...]]] = [
    ("crypto", ("BTC", "ETH", "XRP", "TOTAL")),
    ("equity", ("SPX", "QQQ")),
    ("commodity", ("GOLD", "SILVER", "OIL", "COPPER", "NATGAS")),
]


def _dig(b: dict, *keys):
    """Safely traverse a nested dict; return None if any step is missing."""
    cur = b
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


def cluster_lede(symbol: str, bundles: dict[str, dict],
                 clusters=DEFAULT_CLUSTERS) -> Optional[str]:
    """Peer-group superlative for *symbol* when it wins no GLOBAL superlative.

    Scopes the comparison to *symbol*'s cluster (crypto / equity / commodity) so
    an asset that is merely 'second-most extreme overall' can still earn a
    grounded 'why this one' line within its peers.  Same priority order as
    ``comparative_lede`` but cluster-scoped.

    Returns None when *symbol* is absent, belongs to no cluster, its cluster has
    < 2 present members, or it is not the cluster extreme on any dimension.
    Tied-for-extreme counts (mirrors ``comparative_lede``).
    """
    if symbol not in bundles:
        return None

    word = None
    members = None
    for w, syms in clusters:
        if symbol in syms:
            word = w
            members = [s for s in syms if s in bundles]
            break
    if not members or len(members) < 2:
        return None

    own = bundles[symbol]
    name = asset_name(symbol)

    # priority 1 & 2: range extremes within the cluster
    own_rp = _dig(own, "range_pctile", "pct")
    rps = [_dig(bundles[m], "range_pctile", "pct") for m in members
           if _dig(bundles[m], "range_pctile", "pct") is not None]
    if own_rp is not None and rps:
        if own_rp <= min(rps) and own_rp <= 10:
            return (f"{name} is the weakest of the {word} names — "
                    f"the {ordinal(int(own_rp))} percentile of its 90-day range.")
        if own_rp >= max(rps) and own_rp >= 90:
            return (f"{name} is the strongest of the {word} names — "
                    f"the {ordinal(int(own_rp))} percentile of its 90-day range.")

    # priority 3: most oversold within the cluster
    own_zone = _dig(own, "rsi", "zone")
    own_rsi = _dig(own, "rsi", "value")
    if own_zone == "oversold" and own_rsi is not None:
        osv = [_dig(bundles[m], "rsi", "value") for m in members
               if _dig(bundles[m], "rsi", "zone") == "oversold"
               and _dig(bundles[m], "rsi", "value") is not None]
        if osv and own_rsi <= min(osv):
            return f"{name} is the most oversold of the {word} names — RSI {own_rsi:.0f}."

    # priority 4: longest-held trend within the cluster
    own_bars = _dig(own, "ma_stack", "bars_held")
    if own_bars is not None:
        allb = [_dig(bundles[m], "ma_stack", "bars_held") for m in members
                if _dig(bundles[m], "ma_stack", "bars_held") is not None]
        if allb and own_bars >= max(allb) and own_bars >= 10:
            state = _dig(own, "ma_stack", "state") or ""
            direction = "bull" if "bull" in state else "bear"
            return (f"{name} has held its {direction} stack longest of the {word} names — "
                    f"{own_bars} sessions.")

    return None


# ─────────────────────────────── 3. spotlight_lede ──────────────────────────

def _broad_move_lede(symbol: str, bundles: dict[str, dict]) -> str:
    """Honest range-context line for a range-extreme asset that wins no
    superlative.  Leads with the dominant fact (where it sits in its range) and
    notes cross-asset breadth when the move is broad — never a misleading
    single-session bounce.  Grounded only in the percentiles in *bundles*.
    """
    name = asset_name(symbol)
    own_rp = _dig(bundles.get(symbol, {}), "range_pctile", "pct")
    if own_rp is None:
        return f"{name} held in line with its recent range."
    od = ordinal(int(own_rp))
    if own_rp <= 10:
        n = sum(1 for b in bundles.values()
                if (_dig(b, "range_pctile", "pct") or 50) <= 10)
        tail = ("pinned to its lows alongside much of the tape"
                if n >= 3 else "near the bottom of its range")
        return f"{name} sits at the {od} percentile of its 90-day range — {tail}."
    if own_rp >= 90:
        n = sum(1 for b in bundles.values()
                if (_dig(b, "range_pctile", "pct") or 50) >= 90)
        tail = ("pressed to its highs alongside much of the tape"
                if n >= 3 else "near the top of its range")
        return f"{name} sits at the {od} percentile of its 90-day range — {tail}."
    return f"{name} sits at the {od} percentile of its 90-day range."


def spotlight_lede(symbol: str, bundles: dict[str, dict],
                   clusters=DEFAULT_CLUSTERS) -> str:
    """The card lede — 'why THIS asset', degrading gracefully and never None.

    Chain:
      1. GLOBAL superlative   (``comparative_lede``)
      2. CLUSTER superlative  (``cluster_lede``)
      3. range-extreme → honest breadth-aware range line (``_broad_move_lede``);
         leads with the dominant fact, never a misleading 1-day bounce
      4. otherwise → the salient single-asset sentence (``asset_lede``)
      5. final plain range line
    """
    if symbol not in bundles:
        return ""
    s = comparative_lede(symbol, bundles)
    if s:
        return s
    s = cluster_lede(symbol, bundles, clusters)
    if s:
        return s
    own_rp = _dig(bundles[symbol], "range_pctile", "pct")
    if own_rp is not None and (own_rp <= 10 or own_rp >= 90):
        return _broad_move_lede(symbol, bundles)
    return asset_lede(bundles[symbol]) or _broad_move_lede(symbol, bundles)
