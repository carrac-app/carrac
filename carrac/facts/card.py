"""Structured renderers for the facts-thick "hybrid card".

Pure functions: (bundle dict) → structured rows the template renders.
No I/O, no network, no DB. Every function is gracefully absent — a missing or
None field silently produces no row/entry (never "N/A" filler).

NOTE — intentional omissions (pending precompute additions):
  - weekly RSI: not in the bundle yet (daily rsi block only).
  - "vs 200-day %": price-to-SMA200 gap not yet a precomputed field.
These are documented here so the caller knows they're gaps, not bugs.
"""

from __future__ import annotations

from carrac.facts.labels import level_label, ordinal
from carrac.facts.translate import meaning, rarity_label


# ─────────────────────────────────────────────── helpers ──────────────────────

def _row(label: str, value: str) -> dict:
    return {"label": label, "value": value}


# ════════════════════════════════════════════════════════════════════════════════
# 1. numbers_rows
# ════════════════════════════════════════════════════════════════════════════════



# ════════════════════════════════════════════════════════════════════════════════
# 2. level_flips
# ════════════════════════════════════════════════════════════════════════════════

def _build_flips_for_timeframe(
    streaks: list[dict],
    decisive_closes: list[dict],
) -> list[dict]:
    """Build and sort flip/streak entries for one timeframe (daily or weekly).

    Sort order: decisive closes first (by bars_ago ascending = most recent
    first), then streak entries (longest bars first = most established first).
    """
    entries: list[dict] = []

    # Decisive closes
    for dc in decisive_closes:
        level = dc.get("level", "")
        direction = dc.get("direction", "")
        bars_ago = dc.get("bars_ago", 0) or 0
        lbl = level_label(level)
        if not lbl:
            continue
        if direction == "above":
            text = f"Closed above {lbl} — decisive"
            dir_ = "up"
        elif direction == "below":
            text = f"Lost {lbl}"
            dir_ = "down"
        else:
            text = f"Decisive close through {lbl}"
            dir_ = ""
        entries.append({
            "kind": "decisive",
            "text": text,
            "dir": dir_,
            "_sort_key": (0, bars_ago),  # 0 = decisive closes first
        })

    # Streaks (bars >= 2 per spec)
    for streak in streaks:
        bars = streak.get("bars") or 0
        if bars < 2:
            continue
        level = streak.get("level", "")
        stype = streak.get("type", "")
        lbl = level_label(level)
        if not lbl:
            continue
        text = f"{stype.capitalize()} {lbl} · {bars}d"
        entries.append({
            "kind": "streak",
            "text": text,
            "dir": "",
            "_sort_key": (1, -bars),  # 1 = streaks after decisives; most bars first
        })

    entries.sort(key=lambda e: e["_sort_key"])

    # Strip the internal sort key before returning
    return [{"kind": e["kind"], "text": e["text"], "dir": e["dir"]} for e in entries]


def level_flips(bundle: dict) -> dict[str, list[dict]]:
    """Return ``{"daily": [...], "weekly": [...]}`` level-flip and streak entries.

    Each entry: ``{"kind": str, "text": str, "dir": "up"|"down"|""}``.
    Daily from top-level ``streaks`` + ``decisive_close``.
    Weekly from ``weekly.streaks`` + ``weekly.decisive_close`` (empty if no weekly block).
    """
    daily_streaks = bundle.get("streaks") or []
    daily_dc = bundle.get("decisive_close") or []
    daily = _build_flips_for_timeframe(daily_streaks, daily_dc)

    weekly_block = bundle.get("weekly") or {}
    if weekly_block:
        weekly_streaks = weekly_block.get("streaks") or []
        weekly_dc = weekly_block.get("decisive_close") or []
        weekly = _build_flips_for_timeframe(weekly_streaks, weekly_dc)
    else:
        weekly = []

    return {"daily": daily, "weekly": weekly}


# ════════════════════════════════════════════════════════════════════════════════
# 3. confluence_rows
# ════════════════════════════════════════════════════════════════════════════════



# ════════════════════════════════════════════════════════════════════════════════
# 4. state_rows  — editorial label-first rows (band + meaning channels)
# ════════════════════════════════════════════════════════════════════════════════

def state_rows(bundle: dict) -> list[dict]:
    """Return editorial card rows: ``{"label", "band", "value", "meaning"}``.

    Three channels, no double-counting: VALUE (the number) · BAND (how rare, the
    rarity-label chip, only where it carries signal) · MEANING (plain English).
    Editorial labels (Momentum/Stretch/Participation) over the terminal ones.
    Funding is the SIGN word + percentile band — the historically-extreme
    percentile is the signal; the raw per-interval rate (≈0.000%) is noise.
    Price is intentionally dropped from the spotlight. Rows appear only when
    their source reading is present (never "N/A" filler).
    """
    out: list[dict] = []

    def _push(label: str, band, value: str, metric: str) -> None:
        out.append({"label": label, "band": band, "value": value,
                    "meaning": meaning(metric, bundle)})

    # ── Trend (MA stack) ──────────────────────────────────────────────────────
    ms = bundle.get("ma_stack") or {}
    st = ms.get("state")
    if st and st != "insufficient":
        bars = ms.get("bars_held") or 0
        word = {
            "bullish": "Full bull stack", "bearish": "Full bear stack",
            "bullish_partial": "Partial bull stack", "bearish_partial": "Partial bear stack",
        }.get(st, st)
        _push("Trend", None, f"{word} · {bars}d", "trend")

    # ── Momentum (RSI) ────────────────────────────────────────────────────────
    rv = (bundle.get("rsi") or {}).get("value")
    if rv is not None:
        _push("Momentum", None, f"RSI {rv:.0f}", "rsi")

    # ── Stretch (Mayer) ───────────────────────────────────────────────────────
    stretch = bundle.get("stretch") or {}
    if stretch.get("mayer") is not None and stretch.get("mayer_pct") is not None:
        mp = stretch["mayer_pct"]
        _push("Stretch", rarity_label(mp), f"{stretch['mayer']:.2f}× · {ordinal(mp)}", "mayer")

    # ── Participation (Volume) ────────────────────────────────────────────────
    vol = bundle.get("volume") or {}
    if vol.get("reliable") and vol.get("rvol") is not None:
        _push("Participation", None, f"{vol['rvol']:.1f}× median", "volume")

    # ── Funding (sign + percentile band, NOT the raw rate; crypto only) ───────
    fnd = bundle.get("funding") or {}
    if fnd.get("percentile") is not None:
        signw = "Negative" if fnd.get("sign") == "negative" else "Positive"
        _push("Funding", rarity_label(fnd["percentile"]),
              f"{signw} · {ordinal(fnd['percentile'])}", "funding")

    # ── Range percentile ──────────────────────────────────────────────────────
    rp = (bundle.get("range_pctile") or {}).get("pct")
    if rp is not None:
        _push("Range", rarity_label(rp), f"{ordinal(rp)} of 90 days", "range")

    return out
