"""ANSI QR rendering for the curl /reg funnel page.

Renders a QR matrix to terminal text using vertical half-block glyphs so
two QR rows pack into one text row (a QR cell is ~square only when one
character cell holds two stacked modules).

Scannability: a QR must read as **dark-on-light**. On a dark terminal the
default background is dark, so we can't rely on it for the light (quiet-zone
/ background) modules — a phone camera would see a dark-on-dark blob. We
therefore paint every row with an explicit white background + black
foreground SGR (``\\x1b[30;47m``); the half-block glyphs are then dark
modules on a white field regardless of the user's terminal theme.

The inversion strategy is intentionally isolated in :func:`render_qr` so it
can be swapped after a real phone-scan test (the dark/light SGR pair, or a
full light-on-dark inversion) without touching the router.

``qrcode`` is pure-python here — we only use ``get_matrix()`` and never the
image/PIL path, so Pillow is NOT pulled in.
"""
from __future__ import annotations

# Dark foreground + white background → dark-on-light on any terminal theme.
_SGR_ON = "\x1b[30;47m"
_SGR_OFF = "\x1b[0m"

_FULL = "█"   # █  both halves dark
_UPPER = "▀"  # ▀  top dark only
_LOWER = "▄"  # ▄  bottom dark only
_BLANK = " "       # neither (light, shown via white bg)


def render_qr(url: str, *, border: int = 2) -> str:
    """Return an ANSI half-block QR for *url*, dark-on-light.

    Each output line carries the white-bg/black-fg SGR so the code scans on
    a dark terminal. ~half as many text rows as QR modules (paired).
    """
    import qrcode  # local import: optional-at-import, only needed for /reg

    qr = qrcode.QRCode(border=border)
    qr.add_data(url)
    qr.make()
    matrix = qr.get_matrix()  # list[list[bool]]; True = dark module

    lines: list[str] = []
    for row in range(0, len(matrix), 2):
        top = matrix[row]
        bottom = matrix[row + 1] if row + 1 < len(matrix) else [False] * len(top)
        cells = []
        for t, b in zip(top, bottom):
            if t and b:
                cells.append(_FULL)
            elif t:
                cells.append(_UPPER)
            elif b:
                cells.append(_LOWER)
            else:
                cells.append(_BLANK)
        lines.append(f"{_SGR_ON}{''.join(cells)}{_SGR_OFF}")
    return "\n".join(lines) + "\n"
