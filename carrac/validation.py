"""Shared input validators — hoisted from carrac/journal/routes.py.

Routes (journal, scenarios, plays, alerts) import from here rather than
keeping ad-hoc parsers of their own.

Public API:
    parse_int(data, field, min_val=None, max_val=None) -> int | None
    parse_float(data, field) -> float | None
    parse_json_field(data, field, max_len=...) -> str | None

    SAFE_ASSET_RE         — re.Pattern: matches 1-20 upper-case alnum
    SAFE_TF_RE            — re.Pattern: matches 1m / 4h / 1D / 1W / 1M etc.
    ALLOWED_DIRECTIONS    — frozenset: {'long', 'short'}
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Pattern + enum constants
# ---------------------------------------------------------------------------

SAFE_ASSET_RE = re.compile(r'^[A-Z0-9]{1,20}$')
SAFE_TF_RE = re.compile(r'^[0-9]{0,3}[mhDWM]{1,2}$')
ALLOWED_DIRECTIONS = frozenset({'long', 'short'})

# Default size caps — keep consistent across consumers.
DEFAULT_MAX_JSON_FIELD_LEN = 10_000  # bytes
DEFAULT_MAX_TEXT_FIELD_LEN = 5_000   # chars


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_float(data: Mapping[str, Any], field: str) -> float | None:
    """Safely parse a float from request data. Returns None on any failure
    or on absurd magnitudes (>|1e15|)."""
    val = data.get(field)
    if val is None or val == '':
        return None
    try:
        f = float(val)
        if not (-1e15 < f < 1e15):
            return None
        return f
    except (ValueError, TypeError):
        return None


def parse_int(
    data: Mapping[str, Any],
    field: str,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int | None:
    """Safely parse an int from request data with optional bounds clamp."""
    val = data.get(field)
    if val is None or val == '':
        return None
    try:
        v = int(val)
        if min_val is not None:
            v = max(v, min_val)
        if max_val is not None:
            v = min(v, max_val)
        return v
    except (ValueError, TypeError):
        return None


def parse_json_field(
    data: Mapping[str, Any],
    field: str,
    max_len: int = DEFAULT_MAX_JSON_FIELD_LEN,
) -> str | None:
    """Parse a JSON field. Accepts either a dict/list (re-serialises) or a
    pre-serialised string (validates it parses). Returns None if missing,
    invalid JSON, or over the size cap."""
    val = data.get(field)
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        dumped = json.dumps(val)
        if len(dumped) > max_len:
            return None
        return dumped
    if isinstance(val, str):
        if len(val) > max_len:
            return None
        try:
            json.loads(val)
            return val
        except (json.JSONDecodeError, ValueError):
            return None
    return None
