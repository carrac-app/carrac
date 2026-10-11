"""Tracked-asset whitelist for the curl endpoint.

Lazy-loaded from the asset DB, cached 10 minutes per worker. Resolution is
case-insensitive; the canonical (uppercase) symbol is returned for data calls.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_TTL = 600.0
_lock = threading.Lock()
_symbols: set[str] | None = None
_loaded_at: float = 0.0


def _load_symbols() -> set[str]:
    from database import OHLCVDatabase  # local import: avoid import-time DB access

    db = OHLCVDatabase()
    return {s.lower() for s in db.get_all_symbols()}


def _invalidate() -> None:
    global _symbols, _loaded_at
    with _lock:
        _symbols = None
        _loaded_at = 0.0


def _ensure_loaded() -> set[str] | None:
    """Load (or refresh) the symbol cache if stale. Returns the current set, or None on first-load failure.

    Must be called with _lock held.
    """
    global _symbols, _loaded_at
    now = time.monotonic()
    if _symbols is None or now - _loaded_at > _TTL:
        try:
            # NOTE: load runs under the lock — fine while get_all_symbols() is
            # pure in-memory map work (µs). If it ever grows real I/O, switch
            # to stale-while-refresh so requests don't serialize here.
            _symbols = _load_symbols()
            _loaded_at = now
        except Exception:
            logger.exception("curl registry: symbol load failed")
            # return None only on first-load failure (_symbols still None)
    return _symbols


def resolve(symbol: str) -> str | None:
    """Resolve a symbol (case-insensitive) to its canonical uppercase form, or None if not tracked."""
    with _lock:
        syms = _ensure_loaded()
        if syms is None:
            return None  # fail closed on first load
        return symbol.upper() if symbol.lower() in syms else None


def all_symbols() -> list[str]:
    """Return a sorted uppercase list of all tracked symbols.

    Uses the same lazy-load/TTL cache as ``resolve``. Returns an empty list
    on first-load failure (same fail-closed policy as ``resolve``).
    """
    with _lock:
        syms = _ensure_loaded()
        if syms is None:
            return []
        return sorted(s.upper() for s in syms)
