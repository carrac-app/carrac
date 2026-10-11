"""Pillow share-card renderer — the ``og:image`` for ``/s/<slug>``.

Design constraints (deliberate, see memory carrac-play-shares):
* NO browser/Playwright — pure Pillow text + shapes, ~30ms per card.
* Rendered LAZILY on the first ``/s/<slug>/card.png`` request, persisted to
  ``$DATA_DIR/shared/<slug>.png`` and recorded in ``play_shares.image_path``;
  every later request is a plain file send. ``update_share`` NULLs
  ``image_path``, so toggle changes / snapshot refreshes re-render once.
* Everything drawn from the FROZEN snapshot — no live data, no DB joins,
  deterministic output.
* Never raises into the route: callers catch and fall back to the static
  brand card, so a Pillow hiccup can't break unfurls.

1200×630 (the standard OG size), dark brand theme mirroring share_play.html.
"""

import io
import logging
import os
from pathlib import Path

logger = logging.getLogger('carrac.shares.card')

CARD_W, CARD_H = 1200, 630

# Palette — keep in sync with templates/share_play.html :root.
_BG = '#0d1117'
_PANEL = '#161b22'
_BORDER = '#30363d'
_GREEN = '#3fb950'
_GREEN2 = '#56d364'
_SAGE = '#85b183'  # brand accent — frame/footer only, NOT the
                   # LONG/WIN/up-candle financial-semantic _GREEN uses below.
_TEXT = '#c9d1d9'
_BRIGHT = '#e6edf3'
_MUTED = '#8b949e'
_BLUE = '#58a6ff'
_RED = '#ef5350'

_STATUS_COLORS = {
    'watching': _BLUE,
    'acting': _GREEN,
    'completed': _GREEN2,
    'invalidated': _MUTED,
    'expired': _MUTED,
}

_FONT_DIR = Path(__file__).resolve().parents[2] / 'static' / 'fonts'
_FONT_REGULAR = _FONT_DIR / 'JetBrainsMono-Regular.ttf'
_FONT_BOLD = _FONT_DIR / 'JetBrainsMono-Bold.ttf'


def card_dir():
    """Directory for cached card PNGs (created on demand)."""
    d = os.path.join(os.environ.get('DATA_DIR', 'data'), 'shared')
    os.makedirs(d, exist_ok=True)
    return d


def _font(size, *, bold=False):
    from PIL import ImageFont
    path = _FONT_BOLD if bold else _FONT_REGULAR
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        logger.warning('share card: font %s missing — using PIL default', path.name)
        return ImageFont.load_default(size=size)


def _fit(draw, text, font, max_w):
    """Truncate ``text`` with an ellipsis to fit ``max_w`` pixels."""
    text = (text or '').strip()
    if not text:
        return ''
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + '…', font=font) > max_w:
        text = text[:-1].rstrip()
    return text + '…'


def _wrap2(draw, text, font, max_w):
    """Greedy word-wrap to at most 2 lines, ellipsis on overflow."""
    words = (text or '').split()
    if not words:
        return []
    lines, cur = [], ''
    for w in words:
        cand = f'{cur} {w}'.strip()
        if draw.textlength(cand, font=font) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
            if len(lines) == 2:
                break
    if len(lines) < 2 and cur:
        lines.append(cur)
    if len(lines) > 2 or (len(lines) == 2 and cur and lines[-1] != cur):
        lines = lines[:2]
        lines[-1] = _fit(draw, lines[-1] + ' …', font, max_w)
    return lines[:2]


_MARGIN = 56


def _fmt_price(v):
    """Compact price label: 64,250 / 1.082 / 0.00004523."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ''
    if v >= 1000:
        return f'{v:,.0f}'
    if v >= 1:
        return f'{v:,.6g}'
    return f'{v:.8f}'.rstrip('0')


def _draw_frame(d, share, status_text, status_color, footer_text):
    """Brand frame shared by every card kind: border + top accent, header
    (Carrac.cc wordmark + status pill) and footer (meta line + slug URL).
    The per-kind body renders in between."""
    margin = _MARGIN

    # Frame + top accent
    d.rounded_rectangle([6, 6, CARD_W - 7, CARD_H - 7], radius=18,
                        outline=_BORDER, width=2)
    d.rectangle([6, 6, CARD_W - 7, 12], fill=_SAGE)

    # ── Header: brand left, status pill right ──
    # (No C⁴ mark box — retired; wordmark sits at the margin.)
    f_brand = _font(34, bold=True)
    y = 48
    d.text((margin, y - 2), 'Carrac.cc', font=f_brand, fill=_BRIGHT)

    f_status = _font(26, bold=True)
    sw = d.textlength(status_text, font=f_status)
    sx = CARD_W - margin - sw - 28
    d.rounded_rectangle([sx, y - 4, sx + sw + 28, y + 38], radius=21,
                        outline=status_color, width=2)
    d.text((sx + 14, y), status_text, font=f_status, fill=status_color)

    # ── Footer ──
    fy = CARD_H - 78
    d.line([margin, fy - 16, CARD_W - margin, fy - 16], fill=_BORDER, width=1)
    f_foot = _font(26)
    d.text((margin, fy), footer_text, font=f_foot, fill=_MUTED)

    f_url = _font(28, bold=True)
    from carrac.site_url import site_host
    url = f"{site_host()}/s/{share.get('slug', '')}"
    d.text((CARD_W - margin - d.textlength(url, font=f_url), fy - 2),
           url, font=f_url, fill=_SAGE)


def render_card(share):
    """Render the share's frozen snapshot to a PIL Image (1200×630).

    Dispatches on ``share['kind']`` — each kind draws its body inside the
    shared ``_draw_frame``.
    """
    from PIL import Image

    img = Image.new('RGB', (CARD_W, CARD_H), _BG)
    kind = share.get('kind') or 'play'
    if kind == 'trade':
        _render_trade_card(img, share)
    elif kind == 'chart':
        _render_chart_card(img, share)
    else:
        _render_play_card(img, share)
    return img


def _render_play_card(img, share):
    from PIL import ImageDraw

    snap = share.get('snapshot') or {}
    d = ImageDraw.Draw(img)

    margin = _MARGIN
    inner_w = CARD_W - 2 * margin

    status = (snap.get('status') or 'watching').lower()
    total = snap.get('conditions_total') or 0
    parts = []
    if total:
        parts.append(f"{snap.get('conditions_met', 0)}/{total} conditions met")
    if snap.get('timeframe'):
        parts.append(str(snap['timeframe']).replace('_', ' '))
    _draw_frame(d, share, status.upper(),
                _STATUS_COLORS.get(status, _BLUE),
                '  ·  '.join(parts) or 'Shared market scenario')

    # ── Asset · direction ──
    y = 150
    f_asset = _font(30, bold=True)
    asset = (snap.get('primary_asset') or 'MARKET').upper()
    d.text((margin, y), asset, font=f_asset, fill=_TEXT)
    direction = (snap.get('direction') or '').upper()
    if direction:
        ax = margin + d.textlength(asset, font=f_asset)
        d.text((ax, y), '  ·  ', font=f_asset, fill=_MUTED)
        dx = ax + d.textlength('  ·  ', font=f_asset)
        d.text((dx, y), direction, font=f_asset,
               fill=_GREEN if direction == 'LONG' else _RED)

    # ── Title (≤2 lines) ──
    y = 200
    f_title = _font(52, bold=True)
    for line in _wrap2(d, snap.get('title') or 'Market scenario', f_title, inner_w):
        d.text((margin, y), line, font=f_title, fill=_BRIGHT)
        y += 64

    # ── Body: conditions checklist, else thesis ──
    y = max(y + 24, 350)
    f_body = _font(28)
    conditions = snap.get('conditions') or []
    outcome = snap.get('outcome') or {}
    max_body_y = CARD_H - 120
    if conditions:
        shown = conditions[:4]
        for c in shown:
            if y > max_body_y - 36:
                break
            met = bool(c.get('met'))
            box_c = _GREEN if met else _MUTED
            d.rectangle([margin, y + 5, margin + 22, y + 27], outline=box_c, width=2)
            if met:
                d.rectangle([margin + 5, y + 10, margin + 17, y + 22], fill=_GREEN)
            label = _fit(d, str(c.get('label') or 'condition'), f_body, inner_w - 50)
            d.text((margin + 38, y), label, font=f_body,
                   fill=_TEXT if met else _MUTED)
            y += 42
        if len(conditions) > len(shown):
            d.text((margin + 38, y), f'+ {len(conditions) - len(shown)} more',
                   font=f_body, fill=_MUTED)
            y += 42
    elif snap.get('thesis'):
        for line in _wrap2(d, snap['thesis'], f_body, inner_w)[:2]:
            d.text((margin, y), line, font=f_body, fill=_TEXT)
            y += 38

    # Outcome (opt-in, terminal plays): resolution one-liner.
    if outcome.get('resolution') and y <= max_body_y:
        f_out = _font(26, bold=True)
        res = _fit(d, '▸ ' + str(outcome['resolution']), f_out, inner_w)
        d.text((margin, y + 6), res, font=f_out,
               fill=_GREEN2 if status == 'completed' else _MUTED)


def _stat_tile(d, x, y, w, h, label, value, *, value_color=_BRIGHT):
    """One flagship-card stat tile: muted uppercase label over a bold value,
    inside a subtle panel. Used for the R-multiple / hold-time secondary
    stats — never for the hero number itself."""
    d.rounded_rectangle([x, y, x + w, y + h], radius=14,
                         outline=_BORDER, width=2, fill=_PANEL)
    d.text((x + 24, y + 12), label.upper(), font=_font(21, bold=True), fill=_MUTED)
    d.text((x + 24, y + 38), value, font=_font(38, bold=True), fill=value_color)


def _render_trade_card(img, share):
    """Trade-result body — the flagship flex card.

    Hierarchy: signed P&L % is the one big hero number (with a subtle
    up/down accent triangle); R-multiple + hold time are strong secondary
    stat tiles; asset + a LONG/SHORT pill sit prominent up top; entry→exit
    prices render only when the snapshot carries them (never account
    size / absolute currency P&L — % and R only, always).
    """
    from PIL import ImageDraw

    snap = share.get('snapshot') or {}
    d = ImageDraw.Draw(img)

    margin = _MARGIN
    inner_w = CARD_W - 2 * margin

    pnl = snap.get('pnl_pct')
    win = pnl is not None and pnl > 0
    flat = pnl is not None and pnl == 0
    loss = pnl is not None and not win and not flat
    res_color = _GREEN2 if win else (_MUTED if (flat or pnl is None) else _RED)
    pill = 'WIN' if win else ('FLAT' if flat else ('CLOSED' if pnl is None else 'LOSS'))

    closed = str(snap.get('closed_at') or '')[:10]
    parts = []
    if closed:
        parts.append(f'closed {closed}')
    if snap.get('exit_reason'):
        parts.append(str(snap['exit_reason']).replace('_', ' '))
    if snap.get('timeframe'):
        parts.append(str(snap['timeframe']))
    _draw_frame(d, share, pill, res_color,
                '  ·  '.join(parts) or 'Shared trade result')

    # ── Asset + LONG/SHORT pill — prominent, top of body ──
    y = 150
    f_asset = _font(40, bold=True)
    asset = (snap.get('asset') or 'MARKET').upper()
    d.text((margin, y), asset, font=f_asset, fill=_BRIGHT)
    direction = (snap.get('direction') or '').upper()
    if direction:
        dir_color = _GREEN if direction == 'LONG' else _RED
        f_dir = _font(24, bold=True)
        dx = margin + d.textlength(asset, font=f_asset) + 22
        dw = d.textlength(direction, font=f_dir)
        pad_x = 18
        px0, py0 = dx, y + 8
        px1, py1 = dx + dw + pad_x * 2, y + 8 + 34
        d.rounded_rectangle([px0, py0, px1, py1], radius=17,
                             outline=dir_color, width=2)
        d.text((px0 + pad_x, py0 + 6), direction, font=f_dir, fill=dir_color)

    # ── Hero: signed P&L % — the one big number ──
    hero_y = 225
    f_big = _font(128, bold=True)
    if pnl is None:
        big = 'CLOSED'
    elif flat:
        big = '0.0%'
    else:
        big = f'{pnl:+.1f}%'
    hero_x = margin
    tri_w, tri_gap = 30, 22
    if win or loss:
        hero_x += tri_w + tri_gap
    d.text((hero_x, hero_y), big, font=f_big, fill=res_color)

    if win or loss:
        # Subtle up/down accent triangle beside the hero number — echoes
        # the result color, doesn't compete with it.
        bbox = d.textbbox((hero_x, hero_y), big, font=f_big)
        cy = (bbox[1] + bbox[3]) / 2
        tx0 = margin
        if win:
            tri = [(tx0, cy + 15), (tx0 + tri_w, cy + 15), (tx0 + tri_w / 2, cy - 15)]
        else:
            tri = [(tx0, cy - 15), (tx0 + tri_w, cy - 15), (tx0 + tri_w / 2, cy + 15)]
        d.polygon(tri, fill=res_color)

    # ── Secondary stats: R-multiple + hold time (strong, not competing) ──
    rr = snap.get('rr_actual')
    hold = snap.get('hold')
    tiles_y, tile_h, gap = 400, 74, 24
    tile_w = (inner_w - gap) / 2
    tiles_drawn = rr is not None or hold
    if rr is not None and hold:
        _stat_tile(d, margin, tiles_y, tile_w, tile_h, 'R-MULTIPLE', f'{rr:+.1f}R')
        _stat_tile(d, margin + tile_w + gap, tiles_y, tile_w, tile_h, 'HELD', hold)
    elif rr is not None:
        _stat_tile(d, margin, tiles_y, tile_w, tile_h, 'R-MULTIPLE', f'{rr:+.1f}R')
    elif hold:
        _stat_tile(d, margin, tiles_y, tile_w, tile_h, 'HELD', hold)

    # ── Optional entry → exit (only when the snapshot carries both) ──
    levels = snap.get('levels') or {}
    if levels.get('entry_price') is not None and levels.get('exit_price') is not None:
        ey = tiles_y + tile_h + 20 if tiles_drawn else tiles_y + 12
        f_lab = _font(20, bold=True)
        f_val = _font(28, bold=True)
        entry_s = _fmt_price(levels['entry_price'])
        exit_s = _fmt_price(levels['exit_price'])
        cx = margin
        d.text((cx, ey + 6), 'ENTRY', font=f_lab, fill=_MUTED)
        cx += d.textlength('ENTRY', font=f_lab) + 12
        d.text((cx, ey), entry_s, font=f_val, fill=_TEXT)
        cx += d.textlength(entry_s, font=f_val) + 24
        d.text((cx, ey + 4), '→', font=f_lab, fill=_MUTED)
        cx += d.textlength('→', font=f_lab) + 24
        d.text((cx, ey + 6), 'EXIT', font=f_lab, fill=_MUTED)
        cx += d.textlength('EXIT', font=f_lab) + 12
        d.text((cx, ey), exit_s, font=f_val, fill=_TEXT)


def _render_chart_card(img, share):
    """Chart-moment body: a Pillow candlestick plot (rects + wick lines) from
    the frozen OHLC, with symbol·tf, last price and window %-change in the
    brand frame. v1 draws candles only — no indicator overlays (the line not
    to cross is re-implementing the 49-indicator surface)."""
    from PIL import ImageDraw

    snap = share.get('snapshot') or {}
    d = ImageDraw.Draw(img)
    margin = _MARGIN

    symbol = (snap.get('symbol') or 'MARKET').upper()
    tf = str(snap.get('timeframe') or '').strip()
    change = snap.get('change_pct')
    up = change is not None and change >= 0
    pill_color = _GREEN2 if up else _RED
    pill = (f'{change:+.1f}%' if change is not None else 'CHART')

    # ── Candlestick plot ──
    # _CHART_CARD_CANDLES — card stays readable (snapshots may carry up to
    # 500 bars now for the interactive viewer; the 1200px Pillow sketch is
    # only legible with a tail slice). Hoisted above the footer so the
    # footer's bar count reflects what's actually PLOTTED (up to 120), not
    # the full frozen candle_count (up to 500) — falls back to candle_count
    # for image-only shares that carry no `candles` at all.
    candles = (snap.get('candles') or [])[-120:]
    plotted_bars = len(candles) if candles else snap.get('candle_count', 0)

    foot = [f'{plotted_bars} bars'] if plotted_bars else []
    if snap.get('last_price') is not None:
        foot.append(f'last {_fmt_price(snap["last_price"])}')
    _draw_frame(d, share, pill, pill_color,
                '  ·  '.join(foot) or 'Shared chart')

    # ── Symbol · timeframe ──
    y = 150
    f_sym = _font(40, bold=True)
    d.text((margin, y), symbol, font=f_sym, fill=_BRIGHT)
    if tf:
        sx = margin + d.textlength(symbol, font=f_sym)
        d.text((sx, y + 6), f'  ·  {tf}', font=_font(30, bold=True), fill=_MUTED)

    plot_top, plot_bottom = 220, CARD_H - 110
    plot_left, plot_right = margin, CARD_W - margin
    plot_h = plot_bottom - plot_top
    plot_w = plot_right - plot_left

    rows = []
    for c in candles:
        try:
            o, h, l, cl = (float(c['open']), float(c['high']),
                           float(c['low']), float(c['close']))
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((o, h, l, cl))

    if not rows:
        d.text((margin, plot_top + plot_h // 2), 'No chart data',
               font=_font(28), fill=_MUTED)
        return

    hi = max(r[1] for r in rows)
    lo = min(r[2] for r in rows)
    span = (hi - lo) or (hi or 1.0)

    def _yc(price):
        return plot_bottom - (price - lo) / span * plot_h

    n = len(rows)
    slot = plot_w / n
    body_w = max(1.0, min(slot * 0.7, 14.0))
    for i, (o, h, l, cl) in enumerate(rows):
        cx = plot_left + slot * (i + 0.5)
        col = _GREEN if cl >= o else _RED
        # wick
        d.line([(cx, _yc(h)), (cx, _yc(l))], fill=col, width=1)
        # body
        y0, y1 = _yc(o), _yc(cl)
        top, bot = min(y0, y1), max(y0, y1)
        if bot - top < 1:
            bot = top + 1
        d.rectangle([cx - body_w / 2, top, cx + body_w / 2, bot], fill=col)


def render_capture_card_to_file(share, png_bytes):
    """Composite a CLIENT-CAPTURED chart PNG into the brand frame (option C).

    The captured image is the user's live chart exactly as rendered (every
    drawing + indicator) — we letterbox it into the body band and add the
    brand header + slug footer. Re-encoding through Pillow here is also the
    trust gate: only decodable pixels survive (anything non-image is
    dropped) and it's format-agnostic (PNG or JPEG capture bytes both
    composite fine Chunk 1). Atomic write. Returns the file path.
    """
    from PIL import Image, ImageDraw

    snap = share.get('snapshot') or {}
    canvas = Image.new('RGB', (CARD_W, CARD_H), _BG)

    # Letterbox the capture into the body band (below header, above footer).
    cap = Image.open(io.BytesIO(png_bytes))
    cap.load()                       # force full decode now (validates pixels)
    cap = cap.convert('RGB')
    band_l, band_t, band_r, band_b = _MARGIN, 100, CARD_W - _MARGIN, 524
    bw, bh = band_r - band_l, band_b - band_t
    scale = min(bw / cap.width, bh / cap.height)
    nw, nh = max(1, int(cap.width * scale)), max(1, int(cap.height * scale))
    cap = cap.resize((nw, nh), Image.LANCZOS)
    canvas.paste(cap, (band_l + (bw - nw) // 2, band_t + (bh - nh) // 2))

    d = ImageDraw.Draw(canvas)
    symbol = (snap.get('symbol') or 'CHART').upper()
    tf = str(snap.get('timeframe') or '').strip()
    _draw_frame(d, share, symbol, _SAGE,
                f'{tf} · shared chart' if tf else 'shared chart')

    # Composited cards (a downscaled chart photograph in the frame)
    # are saved as JPEG — a 1200x630 PNG of this content ran ~400KB, over the
    # ~300KB OG budget that keeps mobile/WhatsApp unfurls snappy; q85 JPEG lands
    # ~100-150KB with no visible loss on chart content. The Pillow-sketch
    # fallback (render_card_to_file) stays PNG — crisp vector lines/text, already
    # tiny. The public URL is still /s/<slug>/card.png regardless; unfurlers read
    # the Content-Type header (image/jpeg here), not the URL extension.
    dest = os.path.join(card_dir(), f"{share['slug']}.jpg")
    tmp = f'{dest}.tmp.{os.getpid()}'
    canvas.save(tmp, format='JPEG', quality=85, optimize=True, progressive=True)
    os.replace(tmp, dest)
    return dest


def render_card_to_file(share):
    """Render + persist the card PNG; returns the file path.

    Atomic write (tmp + rename) so a concurrent request never reads a
    half-written file.
    """
    img = render_card(share)
    dest = os.path.join(card_dir(), f"{share['slug']}.png")
    tmp = f'{dest}.tmp.{os.getpid()}'
    img.save(tmp, format='PNG', optimize=True)
    os.replace(tmp, dest)
    return dest
