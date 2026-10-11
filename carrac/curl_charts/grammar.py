"""rate.sx-style grammar for the curl chart endpoint.

Path:   /<symbol>[@<window>]     e.g. /btc@10d
Query:  concatenated flags (?qT) or standard (?q&T&ema&cols=120)
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class GrammarError(ValueError):
    """Bad symbol spec or options — maps to HTTP 400."""


@dataclass(frozen=True)
class Bucket:
    label: str        # canonical window label, e.g. "10d"
    hours: int        # window size
    timeframe: str    # candle TF passed to build_chart_payload
    bars: int         # candle count fetched & rendered


# Canonical buckets — bars sized so bars × timeframe == window exactly AND
# bars ≤ 35 (the renderer fits (cols-10)/2 = 35 candles at the default 80 cols).
BUCKETS: list[Bucket] = [
    Bucket("1h", 1, "5m", 12),
    Bucket("4h", 4, "15m", 16),
    Bucket("1d", 24, "1h", 24),
    Bucket("3d", 72, "4h", 18),
    Bucket("7d", 168, "1D", 7),
    Bucket("10d", 240, "1D", 10),
    Bucket("30d", 720, "1D", 30),
    Bucket("90d", 2160, "1W", 13),
    Bucket("1y", 8760, "1M", 12),
]

_PATH_RE = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9^.-]{0,11})(?:@([A-Za-z0-9]{1,10}))?$")
_WINDOW_RE = re.compile(r"^(\d{1,3})(h|d|w|m|y)$", re.IGNORECASE)
_HOURS = {"h": 1, "d": 24, "w": 168, "m": 720,  # months (30d), not minutes
          "y": 8760}
_ALLOWED_COLS = (80, 100, 120, 160)
_DEFAULT_BUCKET = next(b for b in BUCKETS if b.label == "1d")  # fails fast at import if removed


def parse_path(path: str) -> tuple[str, str | None]:
    """'/btc@10d' → ('btc', '10d'). Raises GrammarError on shape mismatch."""
    m = _PATH_RE.match(path)
    if not m:
        raise GrammarError(f"bad path: {path!r}")
    symbol, window = m.group(1).lower(), m.group(2)
    if window is not None and not _WINDOW_RE.match(window):
        raise GrammarError(f"bad window: {window!r}")
    return symbol, window.lower() if window else None


def snap_window(window: str | None) -> Bucket:
    """Snap a parsed window to the nearest canonical bucket (ties round up)."""
    if window is None:
        return _DEFAULT_BUCKET
    m = _WINDOW_RE.match(window)
    if not m:
        raise GrammarError(f"bad window: {window!r}")
    hours = int(m.group(1)) * _HOURS[m.group(2).lower()]
    if hours == 0:
        raise GrammarError(f"window must be positive: {window!r}")
    best = min(BUCKETS, key=lambda b: (abs(b.hours - hours), -b.hours))
    return best


@dataclass(frozen=True)
class Options:
    ema: bool = False
    plain: bool = False
    quiet: bool = False
    cols: int = 80


def parse_options(query: str) -> Options:
    """Parse the raw query string. Flags: ema, T (plain), q (quiet), cols=N.

    Accepts rate.sx-style concatenation of single-letter flags ('qT')
    and standard '&'-separated form. Unknown tokens raise GrammarError.
    """
    ema = plain = quiet = False
    cols = 80
    for token in filter(None, query.split("&")):
        if token == "ema":
            ema = True
        elif token.startswith("cols="):
            raw = token[5:]
            if not raw.isdigit():
                raise GrammarError(f"bad cols: {token!r}")
            cols = min(_ALLOWED_COLS, key=lambda c: (abs(c - int(raw)), -c))
        elif token and all(ch in "qT" for ch in token):
            quiet = quiet or "q" in token
            plain = plain or "T" in token
        else:
            raise GrammarError(f"unknown option: {token!r}")
    return Options(ema=ema, plain=plain, quiet=quiet, cols=cols)
