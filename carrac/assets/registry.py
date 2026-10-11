"""Asset registry — single source of truth for tradeable symbols and pairs.

One module holds what would otherwise be scattered hardcoded lists across
morning_engine (chart pairs, section categories, ratio assets, asset
categories): one module, one mental model.

Three layers:
1. STATIC defaults (this file) — versioned with the code
2. DB overlay — admin-added/removed assets at runtime, no redeploy
3. Public read API — `chart_pairs()`, `categories()`, `ratio_set()`

The DB overlay tables are introduced lazily by the admin API. Until then,
this module returns the static defaults plus an empty overlay.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

log = logging.getLogger("carrac.assets.registry")

# ----------------------------------------------------------------------
# Static defaults (formerly defined in morning_engine/config.py)
# ----------------------------------------------------------------------

# Format: "<symbol>/<timeframe>". Timeframes: 4H, 1D, 1W.
_STATIC_CHART_PAIRS: tuple[str, ...] = (
    "BTC/4H", "BTC/1D", "BTC/1W",
    "SPX/1D", "SPX/1W",
    "QQQ/1D", "QQQ/1W",
    "OIL/1D", "OIL/1W",
    "GOLD/1D", "GOLD/1W",
    "SILVER/1D", "SILVER/1W",
    "ETH/4H", "ETH/1D", "ETH/1W",
    "XRP/4H", "XRP/1D", "XRP/1W",
    "TOTAL/1D", "TOTAL/1W",
    "MAGS/1D", "MAGS/1W",
    "COPPER/1D", "COPPER/1W",
    "NATGAS/1D", "NATGAS/1W",
    "XLE/1D", "XLE/1W",
    "CORN/1D", "CORN/1W",
    "WHEAT/1D", "WHEAT/1W",
    "SOYBEANS/1D", "SOYBEANS/1W",
    "DXY/1D", "DXY/1W",
    "QQQ_SPX/1D", "QQQ_SPX/1W",
    "GOLD_QQQ/1D", "GOLD_QQQ/1W",
    "MAGS_SPX/1D", "MAGS_SPX/1W",
)

# Section categories.
# Used for depth allocation and report sectioning.
_STATIC_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Crypto": ("BTC", "ETH", "XRP", "TOTAL"),
    "Equities": ("SPX", "QQQ", "MAGS"),
    "Commodities": ("GOLD", "SILVER", "OIL", "COPPER", "NATGAS",
                    "CORN", "WHEAT", "SOYBEANS", "XLE"),
    "Intermarket": ("GOLD_QQQ", "QQQ_SPX", "MAGS_SPX", "DXY"),
}

# Ratio assets. Excluded from spotlight,
# included in cross-asset analysis.
_STATIC_RATIOS: frozenset[str] = frozenset({
    "QQQ_SPX", "GOLD_QQQ", "MAGS_SPX", "DXY",
    "BTC_SPX", "GOLD_BTC", "SPX_GOLD", "ETH_BTC", "XRP_BTC",
})

# ----------------------------------------------------------------------
# DB overlay — populated by the admin API. Empty until then.
# ----------------------------------------------------------------------

@dataclass
class _Overlay:
    added_pairs: set[str] = field(default_factory=set)
    removed_pairs: set[str] = field(default_factory=set)
    added_to_category: dict[str, set[str]] = field(default_factory=dict)
    removed_from_category: dict[str, set[str]] = field(default_factory=dict)
    added_ratios: set[str] = field(default_factory=set)
    removed_ratios: set[str] = field(default_factory=set)

_OVERLAY = _Overlay()
_OVERLAY_LOCK = threading.Lock()


# ----------------------------------------------------------------------
# Public read API — use these from morning_engine and elsewhere.
# ----------------------------------------------------------------------

def chart_pairs() -> list[str]:
    """All chart pairs (static + DB-added − DB-removed)."""
    with _OVERLAY_LOCK:
        result = set(_STATIC_CHART_PAIRS)
        result |= _OVERLAY.added_pairs
        result -= _OVERLAY.removed_pairs
        return sorted(result)


def chart_assets() -> list[str]:
    """Distinct asset symbols (no timeframe) currently configured."""
    return sorted({pair.split("/")[0] for pair in chart_pairs()})


def public_symbol_allowlist() -> set[str] | None:
    """Uppercase symbols allowed on a PUBLIC surface, or None if unavailable.

The free /brief is assembled from caches computed over the
    RS *benchmark* set, which deliberately includes the operator's personal and
    watchlist symbols. Every public consumer of those caches therefore has to
    drop non-registry symbols — and each one that reimplemented the rule got it
    right until the next consumer appeared. Filtering the public RS table alone
    still leaves a watchlist symbol in the public Rotation strip, because
    Rotation reads the same cache and the rule applies there too.

    Returns None — never an empty set — when the registry cannot be read, so a
    caller can PASS THROUGH rather than blank its whole section. An
    over-inclusive band someone notices beats a vanished one nobody can
    explain; that asymmetry is deliberate and is why this is not a bare
    ``set()``.
    """
    try:
        allowed = {s.upper() for s in chart_assets()}
    except Exception as exc:  # noqa: BLE001
        log.warning("registry allowlist unavailable, callers should pass through: %s", exc)
        return None
    if not allowed:
        log.warning("registry allowlist came back EMPTY, callers should pass through")
        return None
    return allowed


def categories() -> dict[str, list[str]]:
    """Section categories with overlay applied."""
    with _OVERLAY_LOCK:
        result = {k: list(v) for k, v in _STATIC_CATEGORIES.items()}
        for cat, adds in _OVERLAY.added_to_category.items():
            result.setdefault(cat, [])
            for a in adds:
                if a not in result[cat]:
                    result[cat].append(a)
        for cat, removes in _OVERLAY.removed_from_category.items():
            if cat in result:
                result[cat] = [a for a in result[cat] if a not in removes]
        return result


def ratio_set() -> frozenset[str]:
    """Set of asset symbols that are ratios (excluded from spotlight)."""
    with _OVERLAY_LOCK:
        result = set(_STATIC_RATIOS)
        result |= _OVERLAY.added_ratios
        result -= _OVERLAY.removed_ratios
        return frozenset(result)


# ----------------------------------------------------------------------
# Admin write API — called by the admin route (POST /api/admin/assets).
# Persists to DB AND updates in-process overlay so changes are immediate
# without restart.
# ----------------------------------------------------------------------

def add_pair(pair: str, category: str | None = None,
             ratio_eligible: bool = False) -> None:
    """Add a chart pair (e.g. 'SOL/1D'). Optionally tag category and ratio flag."""
    if "/" not in pair:
        raise ValueError(f"pair must be 'SYMBOL/TF', got {pair!r}")
    asset = pair.split("/")[0]
    with _OVERLAY_LOCK:
        _OVERLAY.removed_pairs.discard(pair)
        _OVERLAY.added_pairs.add(pair)
        if category:
            _OVERLAY.added_to_category.setdefault(category, set()).add(asset)
            if category in _OVERLAY.removed_from_category:
                _OVERLAY.removed_from_category[category].discard(asset)
        if ratio_eligible:
            _OVERLAY.added_ratios.add(asset)
            _OVERLAY.removed_ratios.discard(asset)
    log.info("registry: added pair %s (category=%s ratio=%s)", pair, category, ratio_eligible)


def remove_pair(pair: str) -> None:
    """Remove a chart pair from active rotation."""
    with _OVERLAY_LOCK:
        _OVERLAY.added_pairs.discard(pair)
        _OVERLAY.removed_pairs.add(pair)
    log.info("registry: removed pair %s", pair)
