"""Helpers for the public ``/assets`` directory + per-asset permalink pages.

The security boundary lives here: ``validate_symbol`` whitelists a path segment
against the curated registry BEFORE it is used as a Redis cache key, so arbitrary
or malicious input (path traversal, injection, overflow) can never reach the
cache/DB. The page surface is read-only over the precomputed facts bundle.
"""

from __future__ import annotations

from carrac.assets.registry import categories, ratio_set
from carrac.facts.labels import asset_name

# Defensive cap — real tickers are short; reject anything longer outright.
_MAX_SYMBOL_LEN = 12


def allowed_symbols():
    """The curated, non-ratio assets that get a standalone ``/assets`` page.
    This list IS the whitelist for ``/assets/<symbol>``."""
    ratios = ratio_set()
    out = []
    for members in categories().values():
        for s in members:
            if s not in ratios and s not in out:
                out.append(s)
    return out


def validate_symbol(raw):
    """Normalize + whitelist-check a path symbol. Returns the canonical symbol,
    or ``None`` when the caller should 404. Defends the cache/DB from arbitrary
    input — nothing outside the curated set is ever honoured."""
    if not raw or not isinstance(raw, str) or len(raw) > _MAX_SYMBOL_LEN:
        return None
    s = raw.strip().upper()
    return s if s in set(allowed_symbols()) else None


def clustered_assets():
    """The directory model: registry clusters → display-named, whitelisted assets.
    Ratios are excluded; order follows the registry."""
    ratios = ratio_set()
    groups = []
    for cluster, members in categories().items():
        assets = [{"symbol": s, "name": asset_name(s)}
                  for s in members if s not in ratios]
        if assets:
            groups.append({"cluster": cluster, "assets": assets})
    return groups
