"""Shareable Plays & Trades — public, frozen-snapshot share links.

A Carrac user mints a short, unguessable public slug for one of their Plays
or closed journal Trades. Anyone can open ``<your-host>/s/<slug>`` with no
Carrac account. The subject is FROZEN into a JSON snapshot at mint time, so
the public artifact is immutable and reveals only the fields the owner opted
to share — even if the underlying row is later edited or deleted.

``play_shares.kind`` selects the share type ('play' | 'trade'; 'chart' is
designed). All kinds share ONE table, slug space
and per-user quota pool.

Routing split (see carrac/asgi.py): writes go to FastAPI (plays router for
play shares, journal router for trade shares — both prefixes are
FastAPI-live); public reads (/s/<slug>) are served by the Flask shares
blueprint, which owns Jinja templates + the marketing base. All call this
module.

Handles both PostgreSQL (production) and SQLite (dev/test) via db_pool
helpers. NOTE: trade rows are read through ``get_content_db()`` — under
SQLite dev/test the journal lives in ``drawings.db`` while shares live in
``carrac.db`` (one PG database in production).
"""

import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone

from carrac.db_pool import (
    adapt_query,
    get_conn,
    get_db_type,
    get_last_id,
    handle_integrity_error,
    returning_id,
)
from carrac.plays.models import get_play

logger = logging.getLogger('carrac.plays.shares')

# Unambiguous alphabet (no 0/o/1/l/i) — Crockford-ish. 8 chars over 31 symbols
# ≈ 31**8 ≈ 8.5e11 space: unguessable, yet short for "<host>/s/<slug>".
_SLUG_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'
_SLUG_LEN = 8
_SLUG_MINT_RETRIES = 5

# Per-tier quota on ACTIVE (non-revoked) shares, kept beside the feature rather
# than in TIER_LIMITS (carrac/auth/decorators.py). The free tier can hold three
# plays, so three shares lets it share every one of them.
SHARE_QUOTA_BY_TIER = {
    'free': 3,
    'member': 25,
    'tester': 25,
    'premium': 999,
    'admin': 999,
}

_BOOL_FIELDS = ('include_thesis', 'include_conditions', 'include_plan',
                'include_outcome')

# Toggle defaults applied at freeze time when the caller didn't set them.
# Outcome (resolution / result) is OFF by default — process-first sharing.
_TOGGLE_DEFAULTS = {
    'include_thesis': True,
    'include_conditions': True,
    'include_plan': True,
    'include_outcome': False,
}

# kind='trade' rides the EXISTING toggle columns (the pshare26060703
# migration adds no new ones): include_levels → include_plan (numeric
# levels are plan data, same semantic as plays), include_note →
# include_thesis (the free-text block). Defaults favour numbers-without-
# account-size: prices ON, the close note OFF.
_TRADE_TOGGLE_DEFAULTS = {
    'include_levels': True,
    'include_note': False,
}


def share_quota(tier):
    """Active-share quota for a tier."""
    return SHARE_QUOTA_BY_TIER.get(tier or 'free', SHARE_QUOTA_BY_TIER['free'])


# ── helpers ──

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(obj):
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    # default=str: on Postgres, play timestamps (created_at/resolved_at) are
    # datetime objects — SQLite hands back TEXT, so tests alone never see
    # this.
    return json.dumps(obj, default=str)


def _json_loads(val):
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return val


def _is_unique_violation(exc):
    try:
        return isinstance(exc, handle_integrity_error())
    except Exception:
        return False


# Canonical SQLite schema (mirrors the alembic chain through pshare26060703).
# play_id is NULLABLE — trade shares set trade_id instead; chart shares
# (designed) will set neither. trade_id carries no REFERENCES clause here:
# under SQLite dev/test trade_journal lives in a different file (drawings.db);
# the real FK + ON DELETE CASCADE exists on Postgres via the migration.
_SHARES_COLS = ('id, kind, play_id, trade_id, user_id, slug, snapshot, '
                'include_thesis, include_conditions, include_plan, '
                'include_outcome, image_path, view_count, created_at, '
                'revoked_at, expires_at')
_CREATE_SHARES_SQL = '''
    CREATE TABLE IF NOT EXISTS play_shares (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind VARCHAR(16) NOT NULL DEFAULT 'play',
        play_id INTEGER,
        trade_id INTEGER,
        user_id INTEGER NOT NULL,
        slug VARCHAR(12) NOT NULL UNIQUE,
        snapshot TEXT NOT NULL DEFAULT '{}',
        include_thesis BOOLEAN DEFAULT 1,
        include_conditions BOOLEAN DEFAULT 1,
        include_plan BOOLEAN DEFAULT 1,
        include_outcome BOOLEAN DEFAULT 0,
        image_path TEXT,
        view_count INTEGER DEFAULT 0,
        created_at TEXT,
        revoked_at TEXT,
        expires_at TEXT
    )
'''


def _ensure_shares_table(conn):
    """Create play_shares if missing (SQLite dev/test only — prod uses alembic)."""
    if get_db_type() != 'sqlite':
        return
    conn.execute(_CREATE_SHARES_SQL)
    # Lightweight dev-DB migration (mirrors alembic pshare26060702/03):
    # long-lived SQLite DBs created before kind/include_outcome/trade_id get
    # the columns added.
    info = conn.execute('PRAGMA table_info(play_shares)').fetchall()
    cols = {r[1] for r in info}
    if 'kind' not in cols:
        conn.execute("ALTER TABLE play_shares ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'play'")
    if 'include_outcome' not in cols:
        conn.execute('ALTER TABLE play_shares ADD COLUMN include_outcome BOOLEAN DEFAULT 0')
    if 'trade_id' not in cols:
        conn.execute('ALTER TABLE play_shares ADD COLUMN trade_id INTEGER')
    # play_id was NOT NULL before pshare26060703; SQLite can't drop NOT NULL
    # in place, so legacy dev DBs get a copy-rebuild (table is tiny).
    if any(r[1] == 'play_id' and r[3] for r in info):
        conn.execute('ALTER TABLE play_shares RENAME TO _play_shares_legacy')
        conn.execute(_CREATE_SHARES_SQL)
        conn.execute(
            f'INSERT INTO play_shares ({_SHARES_COLS}) '
            f'SELECT {_SHARES_COLS} FROM _play_shares_legacy')
        conn.execute('DROP TABLE _play_shares_legacy')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_play_shares_slug ON play_shares(slug)')


def _row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    if 'snapshot' in d:
        d['snapshot'] = _json_loads(d['snapshot']) or {}
    for b in _BOOL_FIELDS:
        if b in d and d[b] is not None:
            d[b] = bool(d[b])
    return d


def _mint_slug():
    return ''.join(secrets.choice(_SLUG_ALPHABET) for _ in range(_SLUG_LEN))


# ── snapshot freezing ──

def _condition_label(rule):
    """Compact, human-readable label for a condition rule (best-effort)."""
    label = rule.get('label')
    if label:
        return label
    t = rule.get('type', '')
    params = rule.get('params', rule)
    if t == 'price_level':
        return f"Price {params.get('relation', '?')} {params.get('level', '?')}"
    if t == 'indicator_state':
        return (f"{params.get('indicator', '?')} {params.get('state', '?')} "
                f"({params.get('timeframe', '?')})")
    if t == 'indicator_value':
        return (f"{params.get('indicator', '?')} ({params.get('timeframe', '?')}) "
                f"{params.get('operator', '?')} {params.get('value', '?')}")
    if t == 'manual':
        return params.get('label', 'Manual check')
    return (t or 'condition').replace('_', ' ')


def _freeze_drawings(drawing_ids, owner_user_id=None):
    """Copy a play's linked drawings into the snapshot (render-only fields).

    Strips user_id / ownership so the public snapshot carries no PII. The
    card render reads these from the snapshot — no live DB join.

    Defense-in-depth: when ``owner_user_id`` is given, any
    drawing not owned by that user is silently dropped — a public share
    must never surface another user's private drawing, even if one
    somehow ended up linked (the primary fix is the ownership check in
    carrac.plays.models.link_drawing; this is a belt-and-suspenders
    freeze-time guard against a bypass or a pre-fix legacy link).
    """
    out = []
    if not drawing_ids:
        return out
    try:
        from carrac.content_db import get_content_db
        db = get_content_db()
        if not db:
            return out
        keep = ('drawing_type', 'price_level', 'price_level_upper', 'label',
                'color', 'line_style', 'line_width', 'metadata', 'asset', 'timeframe')
        for did in drawing_ids:
            try:
                d = db.get_drawing_by_id(int(did))
            except (TypeError, ValueError):
                continue
            if not d:
                continue
            if owner_user_id is not None and d.get('user_id') != owner_user_id:
                logger.warning(
                    'share snapshot: dropping drawing %s not owned by %s',
                    did, owner_user_id)
                continue
            out.append({k: d.get(k) for k in keep if k in d})
    except Exception:
        logger.warning('share snapshot: content_db unavailable; skipping drawings')
    return out


def _num(val):
    """Best-effort float for level fields; None for junk."""
    try:
        f = float(val)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


def _freeze_levels(play):
    """Numeric trade levels from play metadata (entry zone / stop / targets).

    These are PLAN data (the numeric form of the action plan), so they ride
    the include_plan toggle. Sanitized to floats — nothing else leaks through.
    """
    meta = play.get('metadata') or {}
    if not isinstance(meta, dict):
        return None
    levels = {}
    for key in ('entry_low', 'entry_high', 'stop_loss'):
        v = _num(meta.get(key))
        if v is not None:
            levels[key] = v
    targets = [t for t in (_num(x) for x in (meta.get('targets') or [])) if t is not None]
    if targets:
        levels['targets'] = targets
    return levels or None


_TERMINAL_STATUSES = ('completed', 'invalidated')


def _freeze_play_snapshot(play, *, include_thesis, include_conditions,
                          include_plan, include_outcome=False):
    """Build the frozen public snapshot from a play dict + its drawings.

    Reveals only opted-in fields. Carries no user_id / email / ownership data.
    """
    conditions = play.get('conditions') or {'rules': [], 'logic': 'all'}
    rules = conditions.get('rules', []) if isinstance(conditions, dict) else []
    met = sum(1 for r in rules if r.get('met'))
    total = len(rules)

    snap = {
        'title': play.get('title'),
        'primary_asset': play.get('primary_asset'),
        'direction': play.get('direction'),
        'type': play.get('type'),
        'timeframe': play.get('timeframe'),
        'status': play.get('status'),
        'created_at': play.get('created_at'),
        'conditions_met': met,
        'conditions_total': total,
        'chart': {
            'asset': play.get('primary_asset'),
            'timeframe': play.get('timeframe'),
            'indicators': [],   # populated from chart prefs in a later round
            'drawings': _freeze_drawings(play.get('linked_drawing_ids') or [],
                                         owner_user_id=play.get('user_id')),
            # 'candles': [...]  # frozen OHLCV for the sparkline card (later)
        },
    }
    if include_thesis:
        snap['thesis'] = play.get('thesis')
    if include_conditions:
        snap['conditions'] = [
            {'label': _condition_label(r), 'met': bool(r.get('met'))}
            for r in rules
        ]
        snap['conditions_logic'] = (
            conditions.get('logic', 'all') if isinstance(conditions, dict) else 'all')
    if include_plan:
        snap['action_plan'] = play.get('action_plan')
        snap['invalidation'] = play.get('invalidation')
        levels = _freeze_levels(play)
        if levels:
            snap['levels'] = levels
    # Outcome: opt-in AND only meaningful once the play is terminal. R-multiple /
    # P&L will join this block when the journal↔play link lands — server-computed
    # values only (never client-supplied numbers).
    if include_outcome and play.get('status') in _TERMINAL_STATUSES:
        snap['outcome'] = {
            'status': play.get('status'),
            'resolved_at': play.get('resolved_at'),
            'resolution': play.get('resolution'),
        }
    return snap


# ── trade shares (kind='trade') ──

def _get_trade(trade_id, user_id):
    """Owned journal trade, or None. Read via the journal layer
    (``get_content_db``) — NOT raw SQL through db_pool; see module docstring.
    """
    try:
        tid = int(trade_id)
    except (TypeError, ValueError):
        return None
    from carrac.content_db import get_content_db
    trade = get_content_db().get_journal_entry(tid)
    if not trade or trade.get('user_id') != user_id:
        return None
    return trade


def _pnl_pct(trade):
    """Signed percentage move entry→exit, direction-adjusted. Server-side
    only — never client-supplied."""
    entry = _num(trade.get('entry_price'))
    exitp = _num(trade.get('exit_price'))
    if not entry or exitp is None:
        return None
    pct = (exitp - entry) / entry * 100.0
    if (trade.get('direction') or 'long').lower() == 'short':
        pct = -pct
    return round(pct, 2)


def _parse_ts(val):
    """Best-effort tz-aware datetime from a DB timestamp (datetime on
    Postgres, TEXT on SQLite). Naive values are treated as UTC (both
    backends store CURRENT_TIMESTAMP in UTC)."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(val).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _hold_human(opened_at, closed_at):
    """Compact hold-duration label ('3d 14h' / '5h 12m' / '42m'), or None."""
    o, c = _parse_ts(opened_at), _parse_ts(closed_at)
    if not o or not c or c < o:
        return None
    secs = int((c - o).total_seconds())
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {minutes}m'
    return f'{minutes}m'


def _freeze_trade_snapshot(trade, *, include_levels, include_note):
    """Frozen public snapshot for a CLOSED journal trade.

    HARD RULE: absolute position data — ``size`` / ``fees`` /
    ``pnl_currency`` / account tags — NEVER enters the snapshot. The public
    artifact shows % and R, never account size. Whitelist construction below
    enforces this; keep it a whitelist.
    """
    snap = {
        'asset': trade.get('asset'),
        'direction': (trade.get('direction') or 'long').lower(),
        'timeframe': trade.get('timeframe'),
        'pnl_pct': _pnl_pct(trade),
        'rr_actual': _num(trade.get('rr_actual')),
        'exit_reason': trade.get('exit_reason'),
        'opened_at': trade.get('opened_at'),
        'closed_at': trade.get('closed_at'),
        'hold': _hold_human(trade.get('opened_at'), trade.get('closed_at')),
    }
    if include_levels:
        levels = {}
        for key in ('entry_price', 'exit_price'):
            v = _num(trade.get(key))
            if v is not None:
                levels[key] = v
        if levels:
            snap['levels'] = levels
    if include_note and trade.get('notes'):
        snap['note'] = str(trade['notes'])[:500]
    return snap


# ── chart shares (kind='chart') ──

_CHART_CANDLE_LIMIT = 500      # interactive viewer scrollback + indicator warmup
_CHART_CARD_CANDLES = 120      # Pillow OG sketch stays readable at 120
_VIEWPORT_TRAILING_BARS = 20   # context bars after a past-viewport share
_PACK_DATA_KEYS = ('indicators', 'cpr', 'weekly_cpr', 'daily_cpr')
_PACK_DATA_MAX_JSON = 400_000

# Client-captured chart image gate (option C): PNG or JPEG, ≤4 MB decoded
# (generalized from PNG-only — klinecharts'
# getConvertPictureUrl can also emit JPEG for a smaller oversize-retry
# capture). Keyed by data-URL prefix -> required magic bytes; checked in
# order so the first matching prefix wins.
_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
_JPEG_MAGIC = b'\xff\xd8\xff'
_CAPTURE_FORMATS = {
    'data:image/png;base64,': _PNG_MAGIC,
    'data:image/jpeg;base64,': _JPEG_MAGIC,
}
_MAX_CAPTURE_BYTES = 4 * 1024 * 1024


class ChartImageError(ValueError):
    """Raised when a client-supplied chart capture fails the gate."""


def decode_chart_capture(data_url):
    """Validate + decode a client chart capture data URL → image bytes.

    The trust gate for option C: the image is fully client-supplied, so we
    (1) require a supported ``data:image/<fmt>;base64,`` shape (PNG or
    JPEG — see ``_CAPTURE_FORMATS``), (2) cap the decoded size, (3) verify
    the magic bytes match the declared format. The caller then RE-ENCODES
    through Pillow (open→composite→save), which strips anything that isn't
    pixels and is format-agnostic on the way in. Returns the raw image
    bytes, or raises ChartImageError.
    """
    import base64

    if not data_url or not isinstance(data_url, str):
        raise ChartImageError('no image')
    prefix = magic = None
    for candidate_prefix, candidate_magic in _CAPTURE_FORMATS.items():
        if data_url.startswith(candidate_prefix):
            prefix, magic = candidate_prefix, candidate_magic
            break
    if prefix is None:
        raise ChartImageError('image must be a PNG or JPEG data URL')
    b64 = data_url[len(prefix):]
    # Rough decoded-size pre-check before allocating (base64 is ~4/3).
    if len(b64) > _MAX_CAPTURE_BYTES * 4 // 3 + 8:
        raise ChartImageError('image too large')
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        raise ChartImageError('image is not valid base64')
    if len(raw) > _MAX_CAPTURE_BYTES:
        raise ChartImageError('image too large')
    if not raw.startswith(magic):
        raise ChartImageError('image data does not match its declared format')
    return raw


def _freeze_chart_snapshot(symbol, timeframe, *, indicators=None,
                           drawing_ids=None, candle_limit=_CHART_CANDLE_LIMIT,
                           user_id=None, theme=None, viewport=None):
    """Frozen public snapshot for a chart moment.

    Candles are fetched SERVER-SIDE via ``build_chart_payload`` (never
    client-supplied) — the same assembler the live chart uses. We keep up to
    ``candle_limit`` OHLCV bars (windowed around the sharer's viewport when
    given, else the tail) + a window %-change, plus the active indicator
    descriptors and any visible drawings (via the shared ``_freeze_drawings``)
    — enough for the interactive viewer to render the frozen chart, not just
    a static image.

    Returns None when the symbol/timeframe has no data.
    """
    from carrac.charts.service import build_chart_payload

    payload = build_chart_payload(symbol, timeframe=timeframe)
    ohlcv = payload.get('ohlcv') or []
    if not ohlcv:
        return None

    # Merge volume into per-bar dicts by matching `time` — NOT raw list index.
    # build_chart_payload's `volume` list drops NaN-volume bars entirely
    # (carrac/charts/service.py::build_chart_payload), so after any gap an index-based
    # merge pairs every subsequent bar with the wrong volume entry. On
    # COPIES: the payload may come from a shared cache.
    vol_by_time = {}
    for v in (payload.get('volume') or []):
        if isinstance(v, dict) and v.get('time') is not None:
            vol_by_time[v['time']] = v.get('value')
    merged = []
    for c in ohlcv:
        c2 = dict(c)
        t = c2.get('time')
        if t in vol_by_time:
            c2['volume'] = vol_by_time[t]
        merged.append(c2)
    ohlcv = merged

    # Viewport-aware window: cover what the sharer was LOOKING AT (which may
    # be deep history), plus a few trailing bars for context; else the tail.
    vp = None
    if isinstance(viewport, dict):
        try:
            vp = {'from_ts': int(viewport['from_ts']),
                  'to_ts': int(viewport['to_ts'])}
        except (KeyError, TypeError, ValueError):
            vp = None
    if vp:
        upto = [c for c in ohlcv if (c.get('time') or 0) <= vp['to_ts']]
        trailing = [c for c in ohlcv
                    if (c.get('time') or 0) > vp['to_ts']][:_VIEWPORT_TRAILING_BARS]
        candles = (upto + trailing)[-candle_limit:] if upto else ohlcv[-candle_limit:]
    else:
        candles = ohlcv[-candle_limit:]

    first_open = _num(candles[0].get('open'))
    last_close = _num(candles[-1].get('close'))
    change_pct = None
    if first_open and last_close is not None:
        change_pct = round((last_close - first_open) / first_open * 100.0, 2)

    # Server-computed series the indicator pack reads (serverData.indicators /
    # .cpr / .weekly_cpr / .daily_cpr) — whitelisted + size-capped; a share
    # must mint even when this blob is silly-big for some symbol.
    pack = {k: payload.get(k) for k in _PACK_DATA_KEYS if payload.get(k)}
    try:
        if len(json.dumps(pack, default=str)) > _PACK_DATA_MAX_JSON:
            logger.info('chart share: pack_data over %s chars for %s — dropped',
                        _PACK_DATA_MAX_JSON, symbol)
            pack = {}
    except Exception:
        pack = {}

    sym_upper = payload.get('symbol') or str(symbol).upper()
    snap = {
        'symbol': sym_upper,
        'timeframe': payload.get('timeframe') or timeframe,
        'candles': candles,
        'candle_count': len(candles),
        'last_price': last_close,
        'change_pct': change_pct,
        'indicators': _sanitize_chart_indicators(indicators),
        'drawings': _freeze_drawings(drawing_ids or [], owner_user_id=user_id),
        'theme': (str(theme)[:20] if theme else None),
        'viewport': vp,
        'pack_data': pack,
        # Per-asset context tiles, frozen at mint.
        'context': _build_chart_context(sym_upper),
    }
    return snap


def _sanitize_chart_indicators(indicators):
    """Whitelist active-indicator descriptors to {key, params} — never raw
    client objects. Best-effort; bad shapes drop silently."""
    out = []
    if not isinstance(indicators, list):
        return out
    for ind in indicators[:40]:
        if isinstance(ind, str):
            out.append({'key': ind[:40]})
        elif isinstance(ind, dict) and ind.get('key'):
            entry = {'key': str(ind['key'])[:40]}
            params = ind.get('params')
            if isinstance(params, dict):
                clean = {}
                for k, v in list(params.items())[:12]:
                    if isinstance(v, (int, float, str, bool)):
                        clean[str(k)[:20]] = v
                    elif (isinstance(v, list) and len(v) <= 12 and
                          all(isinstance(x, (int, float, str, bool)) for x in v)):
                        clean[str(k)[:20]] = v
                if clean:
                    entry['params'] = clean
            out.append(entry)
    return out


# ── Chunk 2: server-side (Playwright) render for no-capture/MCP shares ──

_SAFE_RENDER_SYMBOL_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,20}$')
_SAFE_RENDER_TF_RE = re.compile(r'^[A-Za-z0-9]{1,5}$')


def _server_render_enabled():
    """Kill switch: default ON."""
    return os.getenv('CHART_SHARE_SERVER_RENDER', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _render_via_screenshot_service(symbol, timeframe, *, width=1600, height=624,
                                   quality=82, timeout=45, share_slug=None, scale=2):
    """Full-indicator headless render via the internal screenshot service.

    Renders at 1600x624 LOGICAL px with device_scale_factor=2 (``scale=2``)
    so the output raster is 3200x1248 (sharp on the full-bleed page), while
    klinecharts lays out its axis / OHLCV legend / price fonts relative to a
    1600px canvas, so they read ~2x bigger than a native-3200 render would
    show them (tiny relative to the chart, then downscaled again on the
    page). Stays under the 3840/side cap (1600*2 = 3200). Quality left at ~80; outer HTTP timeout 45s. This render
    is once-per-mint, never per-view.

    A loopback GET to the SAME keyed endpoint the morning bot / MCP
    chart_get_screenshot use (``screenshot_chart`` in
    carrac/reports/screenshots.py) — mirrors ``screenshot_proxy``'s call
    shape. Used at MINT time for chart shares that have no client capture
    (MCP / headless callers), so the share card gets the real indicator
    stack instead of a candles-only Pillow sketch.

    ``share_slug``: when the share has FROZEN drawings, pass
    its slug so the screenshot service renders via the slug-scoped headless
    page (``/chart-headless/share/<slug>``) instead of the plain
    ``/chart-headless/<asset>/<timeframe>`` page — this is what makes the
    sharer's own drawings show up in the card (the plain page's live
    ``/api/drawings`` fetch 401s under the internal render key and always
    rendered an empty drawing set). Omit it (the default) for the
    no-drawings case — the caller's contract is that this path stays
    byte-identical to before frozen-drawing support when there's nothing to draw.

    Returns the rendered JPEG bytes, or ``None`` on ANY failure — missing
    kill switch / API key, invalid symbol/timeframe, connection error,
    non-200, timeout, or any other exception. NEVER raises: the caller's
    existing lazy Pillow candle-card fallback is the safety net, so a
    slow/unavailable/misconfigured screenshot service must fall through to
    exactly today's behavior, not break the mint.
    """
    if not _server_render_enabled():
        return None

    api_key = os.getenv('SCREENSHOT_API_KEY', '')
    if not api_key:
        return None

    symbol = (symbol or '').strip().upper()
    timeframe = (timeframe or '').strip()
    if not _SAFE_RENDER_SYMBOL_RE.match(symbol) or not _SAFE_RENDER_TF_RE.match(timeframe):
        return None

    try:
        import requests

        from carrac.reports.screenshots import _DEFAULT_SCREENSHOT_INDICATORS

        params = {
            'width': width,
            'height': height,
            'quality': quality,
            'scale': scale,
            'indicators': _DEFAULT_SCREENSHOT_INDICATORS,
        }
        if share_slug:
            params['share_slug'] = share_slug

        resp = requests.get(
            f'http://localhost:5000/api/screenshot/{symbol}/{timeframe}',
            params=params,
            headers={'X-Internal-Key': api_key},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.info(
                'chart share server-render: screenshot service returned %s for %s/%s',
                resp.status_code, symbol, timeframe)
            return None
        return resp.content
    except Exception:
        logger.warning(
            'chart share server-render: screenshot service call failed for %s/%s',
            symbol, timeframe, exc_info=True)
        return None


# ── Chunk 3: per-asset context (replaces the generic brochure on chart shares) ──

def _chart_context_confluence(symbol):
    """Nearest support/resistance zone (scanner confluence cache), or None."""
    from carrac.scanner.confluence_query import get_symbol_confluence

    payload, status = get_symbol_confluence(symbol)
    if status != 'ok' or not payload:
        return None
    price = payload.get('price') or 0
    candidates = []
    supports = payload.get('supports') or []
    resistances = payload.get('resistances') or []
    if supports:
        candidates.append(('support', supports[0]))
    if resistances:
        candidates.append(('resistance', resistances[0]))
    if not candidates:
        return None
    if price:
        side, zone = min(
            candidates, key=lambda c: abs((c[1].get('price') or 0) - price))
    else:
        side, zone = candidates[0]
    return {
        'side': side,
        'price': zone.get('price'),
        'label': zone.get('label_friendly') or zone.get('label'),
        'strength': zone.get('strength'),
        'tier': zone.get('tier'),
    }


def _chart_context_scanner(symbol):
    """Scanner verdict/score/direction for the symbol, or None.

    Field names verified against carrac/scanner/scorer/engine.py's result
    shape (best/best_score/best_verdict) — same fields get_scanner_detail
    (carrac/scanner/routes.py) reads.
    """
    from carrac.caches import get_cached_scanner

    data = get_cached_scanner(allow_stale=True)
    if not data:
        return None
    for result in data.get('results', []):
        if result.get('symbol') == symbol:
            return {
                'verdict': result.get('best_verdict'),
                'score': result.get('best_score'),
                'direction': result.get('best'),
            }
    return None


def _chart_context_regime_asset(symbol):
    """Rich derivatives regime for BTC/ETH — single-row quant.derivatives
    read + classify, scoped to THIS asset. Shares its fetch + classify
    mapping with carrac/mcp/tools/quant.py quant_get_regime via
    quant.engine.derivatives_regime.fetch_latest_derivatives_row /
    classify_asset_row (hand-rolling the query at each call site drifts —
    the MCP tool once always classified BTC's row regardless of the requested
    asset, which would be wrong here too for a per-asset ETH context tile)."""
    from quant.engine.derivatives_regime import (
        classify_asset_row, fetch_latest_derivatives_row)

    row = fetch_latest_derivatives_row(symbol)
    if not row:
        return None
    regime = classify_asset_row(row)
    if not regime:
        return None
    label = regime.get('regime_name') or regime.get('regime')
    if not label or label == 'unknown':
        return None
    return {'label': label, 'confidence': regime.get('confidence'), 'scope': 'asset'}


def _chart_context_regime_global():
    """Global macro HMM regime — labeled market-wide, NEVER asset-specific
    (a deliberate choice): every non-BTC/ETH symbol gets this same
    fact. Pattern from quant/check_alerts.py _fetch_current_regime, reused
    directly rather than re-querying quant.signals here."""
    from quant.check_alerts import _fetch_current_regime

    label = _fetch_current_regime()
    if not label or label == 'unknown':
        return None
    return {'label': label, 'scope': 'market'}


# Regime is the only Chunk-3 fact that crosses the network to Postgres (quant
# pool has no configured connect_timeout — carrac/db_pool.py _make_pool).
# Confluence/scanner read the existing shared in-process/Redis scanner cache
# that many other live endpoints already depend on synchronously, so they're
# left as-is; this bound is specific to the NEW network dependency.
_CHART_CONTEXT_REGIME_TIMEOUT_SEC = 3.0


def _run_with_deadline(fn, timeout_sec):
    """Run ``fn`` (no args) with a hard wall-clock deadline.

    Chart-share mint runs SYNCHRONOUSLY at request time (a deliberate choice),
    so a Postgres connectivity blip on the regime read (DNS failure, a
    black-holed route, a saturated pool) must not stall the whole mint on
    the OS-level TCP connect timeout (which can run into tens of seconds
    with no configured ``connect_timeout``). try/except alone only bounds
    FAILURES, not TIME, so this bounds the wait explicitly.

    Raises ``concurrent.futures.TimeoutError`` past the deadline — caught by
    the same per-fact try/except in ``_build_chart_context`` as any other
    failure. The background thread isn't forcibly killed (Python can't do
    that); it just finishes on its own and the caller stops waiting on it.
    """
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_sec)
    finally:
        executor.shutdown(wait=False)


def _build_chart_context(symbol):
    """Per-asset market context frozen into a chart share's snapshot AT MINT
    time — replaces the generic feature brochure with
    real, symbol-specific facts: nearest confluence zone, scanner verdict/
    score, and market regime.

    Frozen ONCE per mint (matches the frozen-moment share contract —
    ``get_symbol_confluence`` alone can cost ~1.3s on a cache-cold scanner
    blob, so this must be O(mints), never O(views)).

    Each of the three facts is independently best-effort: a source failing
    (cold cache, missing row, import error, DB down, ...) just omits that
    one key so a mint NEVER fails because of this. Returns ``{}`` when every
    source fails — the template's ``{% if snap.context %}`` guard then hides
    the whole block (same guard also protects pre-Chunk-3 rows that have no
    'context' key at all).

    A deliberate choice: scanner/confluence facts, though
    gated to logged-in members on their LIVE endpoints, are approved to show
    anonymous share viewers here — these are frozen, mint-time values, not a
    live public API.
    """
    symbol = (symbol or '').upper().strip()
    context = {}
    if not symbol:
        return context

    try:
        zone = _chart_context_confluence(symbol)
        if zone:
            context['confluence'] = zone
    except Exception:
        logger.warning('chart share context: confluence lookup failed for %s',
                       symbol, exc_info=True)

    try:
        scanner = _chart_context_scanner(symbol)
        if scanner:
            context['scanner'] = scanner
    except Exception:
        logger.warning('chart share context: scanner lookup failed for %s',
                       symbol, exc_info=True)

    try:
        if symbol in ('BTC', 'ETH'):
            regime = _run_with_deadline(
                lambda: _chart_context_regime_asset(symbol),
                _CHART_CONTEXT_REGIME_TIMEOUT_SEC)
        else:
            regime = _run_with_deadline(
                _chart_context_regime_global,
                _CHART_CONTEXT_REGIME_TIMEOUT_SEC)
        if regime:
            context['regime'] = regime
    except Exception:
        logger.warning('chart share context: regime lookup failed for %s',
                       symbol, exc_info=True)

    return context


# ── CRUD ──

def get_share_by_id(share_id):
    with get_conn() as conn:
        _ensure_shares_table(conn)
        cur = conn.execute(
            adapt_query('SELECT * FROM play_shares WHERE id = ?'), (share_id,))
        return _row_to_dict(cur.fetchone())


def get_share_by_slug(slug, *, increment_views=False):
    """Public fetch by slug. Returns the share dict or None.

    The caller decides HTTP status from the returned dict:
      None             -> 404 (unknown slug)
      revoked_at set   -> 410 Gone
      expired          -> 404
    """
    with get_conn() as conn:
        _ensure_shares_table(conn)
        cur = conn.execute(
            adapt_query('SELECT * FROM play_shares WHERE slug = ?'), (slug,))
        row = _row_to_dict(cur.fetchone())
        if row and increment_views and not row.get('revoked_at'):
            conn.execute(
                adapt_query('UPDATE play_shares SET view_count = view_count + 1 WHERE id = ?'),
                (row['id'],))
    return row


def get_active_share_for_play(play_id, user_id):
    """Most recent non-revoked share for a play owned by user_id, or None."""
    with get_conn() as conn:
        _ensure_shares_table(conn)
        cur = conn.execute(
            adapt_query(
                'SELECT * FROM play_shares WHERE play_id = ? AND user_id = ? '
                'AND revoked_at IS NULL ORDER BY id DESC'),
            (play_id, user_id))
        return _row_to_dict(cur.fetchone())


def get_active_share_for_trade(trade_id, user_id):
    """Most recent non-revoked trade share owned by user_id, or None."""
    with get_conn() as conn:
        _ensure_shares_table(conn)
        cur = conn.execute(
            adapt_query(
                'SELECT * FROM play_shares WHERE trade_id = ? AND user_id = ? '
                'AND revoked_at IS NULL ORDER BY id DESC'),
            (trade_id, user_id))
        return _row_to_dict(cur.fetchone())


def count_active_shares(user_id):
    """Count non-revoked shares for tier-quota enforcement."""
    with get_conn() as conn:
        _ensure_shares_table(conn)
        cur = conn.execute(
            adapt_query(
                'SELECT COUNT(*) AS cnt FROM play_shares '
                'WHERE user_id = ? AND revoked_at IS NULL'),
            (user_id,))
        row = cur.fetchone()
        return row['cnt'] if isinstance(row, dict) else row[0]


def create_share(user_id, play_id, *, include_thesis=None,
                 include_conditions=None, include_plan=None,
                 include_outcome=None, reuse_existing=True):
    """Mint (or return the existing active) share for a play owned by user_id.

    Toggle args use None = "caller didn't say" — defaults from
    ``_TOGGLE_DEFAULTS`` apply on a fresh mint. When an active share already
    exists AND the caller passed explicit toggles, they are APPLIED (the
    snapshot re-freezes) instead of being silently ignored.

    Returns the share dict, or None if the play isn't found / not owned.
    Quota is enforced by the caller (FastAPI endpoint) before minting.
    """
    play = get_play(play_id, user_id)
    if not play:
        return None

    explicit = {
        'include_thesis': include_thesis,
        'include_conditions': include_conditions,
        'include_plan': include_plan,
        'include_outcome': include_outcome,
    }

    if reuse_existing:
        existing = get_active_share_for_play(play_id, user_id)
        if existing:
            if any(v is not None for v in explicit.values()):
                return update_share(play_id, user_id, **explicit)
            return existing

    toggles = {k: _TOGGLE_DEFAULTS[k] if v is None else bool(v)
               for k, v in explicit.items()}
    snapshot = _freeze_play_snapshot(play, **toggles)
    return _mint_share('play', user_id, snapshot, toggles, play_id=play_id)


def _mint_share(kind, user_id, snapshot, cols, *, play_id=None, trade_id=None):
    """Insert a share row with a fresh slug (collision-retried).

    ``cols`` holds the four DB toggle columns (play names — trade callers
    map their kind-facing toggles before calling). Returns the share dict.
    """
    now = _now_iso()
    last_err = None
    for _ in range(_SLUG_MINT_RETRIES):
        slug = _mint_slug()
        try:
            with get_conn() as conn:
                _ensure_shares_table(conn)
                sql = adapt_query('''
                    INSERT INTO play_shares (
                        kind, play_id, trade_id, user_id, slug, snapshot,
                        include_thesis, include_conditions, include_plan,
                        include_outcome, view_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''') + returning_id()
                cur = conn.execute(sql, (
                    kind, play_id, trade_id, user_id, slug,
                    _json_dumps(snapshot),
                    cols['include_thesis'], cols['include_conditions'],
                    cols['include_plan'], cols['include_outcome'],
                    0, now,
                ))
                share_id = get_last_id(cur, conn)
            return get_share_by_id(share_id)
        except Exception as exc:  # noqa: BLE001 — slug collision retry
            if _is_unique_violation(exc):
                last_err = exc
                continue
            raise
    raise RuntimeError(
        f'could not mint a unique share slug after {_SLUG_MINT_RETRIES} tries: {last_err}')


def create_trade_share(user_id, trade_id, *, include_levels=None,
                       include_note=None, reuse_existing=True):
    """Mint (or return the existing active) share for a CLOSED trade.

    Same semantics as ``create_share``: toggle args use None = "caller
    didn't say"; explicit toggles APPLY on reuse (snapshot re-freezes).
    Returns None when the trade isn't found / not owned / not closed —
    callers pre-check closed-ness to give a distinct 400.
    Quota is enforced by the caller.
    """
    trade = _get_trade(trade_id, user_id)
    if not trade or not trade.get('closed_at'):
        return None

    explicit = {'include_levels': include_levels, 'include_note': include_note}

    if reuse_existing:
        existing = get_active_share_for_trade(trade_id, user_id)
        if existing:
            if any(v is not None for v in explicit.values()):
                return update_trade_share(trade_id, user_id, **explicit)
            return existing

    toggles = {k: _TRADE_TOGGLE_DEFAULTS[k] if v is None else bool(v)
               for k, v in explicit.items()}
    snapshot = _freeze_trade_snapshot(trade, **toggles)
    cols = {
        'include_thesis': toggles['include_note'],     # note rides thesis col
        'include_conditions': False,                   # unused for trades
        'include_plan': toggles['include_levels'],     # levels ride plan col
        'include_outcome': True,                       # a result card IS the outcome
    }
    return _mint_share('trade', user_id, snapshot, cols, trade_id=trade_id)


def update_trade_share(trade_id, user_id, *, include_levels=None,
                       include_note=None):
    """Update visibility toggles on the active trade share and RE-FREEZE the
    snapshot from the trade's current state. Invalidates the cached card PNG.

    Returns the updated share, or None without an active share / closed trade.
    """
    share = get_active_share_for_trade(trade_id, user_id)
    if not share:
        return None
    trade = _get_trade(trade_id, user_id)
    if not trade or not trade.get('closed_at'):
        return None

    lv = share['include_plan'] if include_levels is None else bool(include_levels)
    nt = share['include_thesis'] if include_note is None else bool(include_note)

    snapshot = _freeze_trade_snapshot(trade, include_levels=lv, include_note=nt)

    with get_conn() as conn:
        _ensure_shares_table(conn)
        conn.execute(
            adapt_query(
                'UPDATE play_shares SET include_thesis = ?, include_plan = ?, '
                'snapshot = ?, image_path = NULL WHERE id = ?'),
            (nt, lv, _json_dumps(snapshot), share['id']))
    return get_share_by_id(share['id'])


def revoke_trade_share(trade_id, user_id):
    """Revoke all active shares for a trade owned by user_id. Returns count."""
    now = _now_iso()
    with get_conn() as conn:
        _ensure_shares_table(conn)
        cur = conn.execute(
            adapt_query(
                'UPDATE play_shares SET revoked_at = ? '
                'WHERE trade_id = ? AND user_id = ? AND revoked_at IS NULL'),
            (now, trade_id, user_id))
        return cur.rowcount


def _save_full_image(slug, raw_bytes):
    """Persist the RAW, pre-composite chart image as the page hero
    (``<slug>_full.<ext>``), alongside the branded card in the same
    ``card_dir()``.

    Card compositing (``render_capture_card_to_file``) letterboxes these same
    bytes down into the 1200x630 brand frame for the og:image; this saves the
    UNCOMPOSITED bytes so the public page can show them at full resolution
    instead (served by ``/s/<slug>/chart.jpg``).

    Extension follows the ACTUAL bytes — JPEG from the server-render path,
    PNG *or* JPEG from a client capture — rather than a fixed name, and that
    filename doubles as the only "is there a hero image" record: no new DB
    column or snapshot key, just a presence + suffix check at page-render
    time (``carrac.shares.routes._find_full_image``). The public URL stays
    ``.jpg`` regardless (simplicity for callers/CDNs); the served
    Content-Type follows whichever file is actually found on disk.

    Atomic write (tmp + rename), matching every other card-dir writer in this
    codebase. Best-effort by design — callers wrap this in their own
    try/except so a hero-save failure never blocks the mint or the card.
    """
    from carrac.shares.card import card_dir

    ext = 'png' if raw_bytes.startswith(_PNG_MAGIC) else 'jpg'
    dest = os.path.join(card_dir(), f'{slug}_full.{ext}')
    tmp = f'{dest}.tmp.{os.getpid()}'
    with open(tmp, 'wb') as f:
        f.write(raw_bytes)
    os.replace(tmp, dest)
    return dest


def create_chart_share(user_id, symbol, timeframe, *, capture_png=None,
                       indicators=None, drawing_ids=None, theme=None,
                       viewport=None):
    """Mint a NEW chart share (a frozen moment in time).

    Two paths:
    * ``capture_png`` (option C, the web UI): the client-rendered chart PNG —
      every drawing + indicator exactly as displayed. Composited into the
      brand frame at mint and stored as the card (full fidelity, no re-render).
      The structured candle snapshot is ALSO frozen alongside the image (via
      ``_freeze_chart_snapshot``) so the interactive viewer has data to
      render even on this path; degrades to an image-only snapshot if that
      freeze fails.
    * no capture (MCP / headless): first tries a
      full-indicator Playwright render via the internal screenshot service
      (``_render_via_screenshot_service``) synchronously at mint time; if
      that's disabled/unavailable/fails for ANY reason, the server falls
      back to today's behavior — candles assembled via ``build_chart_payload``
      and rendered as a lazy Pillow candle card (v1, no indicator overlays).

    Both paths freeze a per-asset context block (``_build_chart_context`` —
    nearest confluence zone, scanner verdict, market regime) into the snapshot, replacing the generic feature brochure on
    the public page.

    Unlike plays/trades there is NO get-or-create: every share is a distinct
    moment, and no subject row exists (play_id / trade_id both NULL). Quota
    (enforced by the caller) bounds the volume.

    Returns the share dict, or None when there's nothing to render.
    """
    cols = {
        'include_thesis': False,
        'include_conditions': False,
        'include_plan': False,
        'include_outcome': False,
    }

    if capture_png is not None:
        # The interactive viewer needs the structured payload on the capture
        # path too. Degrade to the image-only snapshot if the freeze fails —
        # a data hiccup must not break a share that HAS a perfect capture.
        try:
            snapshot = _freeze_chart_snapshot(
                symbol, timeframe, indicators=indicators,
                drawing_ids=drawing_ids, user_id=user_id,
                theme=theme, viewport=viewport)
        except Exception:
            logger.exception('chart capture: structured freeze failed for %s '
                             '%s — image-only share', symbol, timeframe)
            snapshot = None
        if snapshot is None:
            snapshot = {
                'symbol': str(symbol).upper(),
                'timeframe': timeframe,
                'context': _build_chart_context(symbol),
            }
        snapshot['source'] = 'capture'
        share = _mint_share('chart', user_id, snapshot, cols)
        # Composite into the brand frame NOW and pin image_path so the card
        # route serves the file directly (skips the lazy candle render).
        try:
            from carrac.shares.card import render_capture_card_to_file
            path = render_capture_card_to_file(share, capture_png)
            set_image_path(share['id'], path)
            share['image_path'] = path
        except Exception:
            logger.exception('chart capture composite failed for %s — '
                             'card will fall back to static og', share['slug'])
        # Also stash the raw capture bytes as the page hero — same
        # bytes the card was just composited from, independent try/except so
        # a hero-save hiccup can't take the (already-succeeded) card down
        # with it, or vice versa.
        try:
            _save_full_image(share['slug'], capture_png)
        except Exception:
            logger.warning('chart capture: failed to save full-res hero image '
                           'for %s — page falls back to card_url', share['slug'],
                           exc_info=True)
        return share

    snapshot = _freeze_chart_snapshot(
        symbol, timeframe, indicators=indicators, drawing_ids=drawing_ids,
        user_id=user_id, theme=theme, viewport=viewport)
    if not snapshot:
        return None
    share = _mint_share('chart', user_id, snapshot, cols)

    # Try the full-indicator server render before falling
    # back to the lazy Pillow candle card. _render_via_screenshot_service's
    # own contract is "returns bytes or None, never raises" — this outer
    # try/except is belt-and-suspenders so an unexpected bug in that helper
    # degrades the SAME way a clean None would, rather than 500ing the mint.
    try:
        # When the snapshot has frozen drawings, request the
        # slug-scoped headless render so the card shows the sharer's own
        # drawings — the plain asset/timeframe headless page can't (its
        # live /api/drawings fetch 401s under the internal render key).
        # No drawings -> no share_slug kwarg at all -> call stays byte-
        # identical to the pre-frozen-drawing behaviour.
        render_kwargs = {'share_slug': share['slug']} if snapshot.get('drawings') else {}
        screenshot_bytes = _render_via_screenshot_service(
            snapshot['symbol'], snapshot['timeframe'], **render_kwargs)
    except Exception:
        logger.exception(
            'chart share: _render_via_screenshot_service raised unexpectedly '
            'for %s — falling back to the lazy candle card', share['slug'])
        screenshot_bytes = None
    if screenshot_bytes:
        try:
            from carrac.shares.card import render_capture_card_to_file
            path = render_capture_card_to_file(share, screenshot_bytes)
            # THE easy-to-forget step: flip + PERSIST the
            # snapshot's source so the public page's `snap.source ==
            # 'server_render'` branch matches the image we just wrote —
            # without this the card.png is the full render but the page
            # still shows the bare candles branch (image/page mismatch).
            snapshot['source'] = 'server_render'
            _persist_snapshot(share['id'], snapshot)
            set_image_path(share['id'], path)
            share['snapshot'] = snapshot
            share['image_path'] = path
        except Exception:
            logger.exception(
                'chart share server-render composite failed for %s — '
                'falling back to the lazy candle card', share['slug'])
        # Also stash the raw (3200x1248) render as the page
        # hero — same bytes the card was just composited from. Independent
        # try/except, same reasoning as the capture path above.
        try:
            _save_full_image(share['slug'], screenshot_bytes)
        except Exception:
            logger.warning(
                'chart share server-render: failed to save full-res hero '
                'image for %s — page falls back to card_url', share['slug'],
                exc_info=True)

    return share


def revoke_share_by_slug(slug, user_id):
    """Revoke a single share by slug, scoped to its owner. Used by chart
    shares (no subject id to revoke by). Returns count revoked (0 or 1)."""
    now = _now_iso()
    with get_conn() as conn:
        _ensure_shares_table(conn)
        cur = conn.execute(
            adapt_query(
                'UPDATE play_shares SET revoked_at = ? '
                'WHERE slug = ? AND user_id = ? AND revoked_at IS NULL'),
            (now, slug, user_id))
        return cur.rowcount


def update_share(play_id, user_id, *, include_thesis=None,
                 include_conditions=None, include_plan=None,
                 include_outcome=None):
    """Update field-visibility toggles on the active share and RE-FREEZE the
    snapshot from the play's CURRENT state. Invalidates the cached card PNG.

    Calling with all toggles None is the explicit "refresh snapshot" path —
    toggles keep their stored values, the snapshot re-freezes from the play.

    Returns the updated share, or None if there is no active share / play.
    """
    share = get_active_share_for_play(play_id, user_id)
    if not share:
        return None
    play = get_play(play_id, user_id)
    if not play:
        return None

    it = share['include_thesis'] if include_thesis is None else bool(include_thesis)
    ic = share['include_conditions'] if include_conditions is None else bool(include_conditions)
    ip = share['include_plan'] if include_plan is None else bool(include_plan)
    io_ = share.get('include_outcome', False) if include_outcome is None else bool(include_outcome)

    snapshot = _freeze_play_snapshot(
        play, include_thesis=it, include_conditions=ic, include_plan=ip,
        include_outcome=io_)

    with get_conn() as conn:
        _ensure_shares_table(conn)
        conn.execute(
            adapt_query(
                'UPDATE play_shares SET include_thesis = ?, include_conditions = ?, '
                'include_plan = ?, include_outcome = ?, snapshot = ?, '
                'image_path = NULL WHERE id = ?'),
            (it, ic, ip, io_, _json_dumps(snapshot), share['id']))
    return get_share_by_id(share['id'])


def get_shares_for_plays(user_id, play_ids):
    """Map of play_id -> lightweight active-share info for list enrichment.

    One IN-clause query; powers the ``share`` field on ``list_plays`` so the
    UI + MCP can show shared-state without extra round trips.
    """
    ids = [int(p) for p in play_ids if p is not None]
    if not ids:
        return {}
    placeholders = ', '.join('?' for _ in ids)
    with get_conn() as conn:
        _ensure_shares_table(conn)
        cur = conn.execute(
            adapt_query(
                f'SELECT play_id, slug, view_count, created_at FROM play_shares '
                f'WHERE user_id = ? AND revoked_at IS NULL AND play_id IN ({placeholders}) '
                f'ORDER BY id'),
            (user_id, *ids))
        out = {}
        for row in cur.fetchall():
            d = dict(row)
            # Multiple actives per play shouldn't happen (mint reuses), but if
            # they do, the LAST row (highest id) wins — matches
            # get_active_share_for_play's ORDER BY id DESC pick.
            out[d['play_id']] = {
                'slug': d['slug'],
                'path': f"/s/{d['slug']}",
                'view_count': d.get('view_count', 0),
            }
        return out


def revoke_share(play_id, user_id):
    """Revoke all active shares for a play owned by user_id. Returns count revoked."""
    now = _now_iso()
    with get_conn() as conn:
        _ensure_shares_table(conn)
        cur = conn.execute(
            adapt_query(
                'UPDATE play_shares SET revoked_at = ? '
                'WHERE play_id = ? AND user_id = ? AND revoked_at IS NULL'),
            (now, play_id, user_id))
        return cur.rowcount


def set_image_path(share_id, path):
    """Record the cached card-PNG path."""
    with get_conn() as conn:
        _ensure_shares_table(conn)
        conn.execute(
            adapt_query('UPDATE play_shares SET image_path = ? WHERE id = ?'),
            (path, share_id))


def _persist_snapshot(share_id, snapshot):
    """Persist an updated frozen snapshot JSON blob for an existing share row.

    Easy-to-forget step: after a server-render
    composite succeeds, the in-memory ``snapshot['source']`` flip to
    ``'server_render'`` must ALSO land in the DB row, or the public page (which
    reads the persisted snapshot on every request) keeps rendering the
    candles branch while ``card.png`` is already the full render — an
    image/page mismatch. No toggle columns touched here (chart shares carry
    no toggles); pair with ``set_image_path`` for the full update.
    """
    with get_conn() as conn:
        _ensure_shares_table(conn)
        conn.execute(
            adapt_query('UPDATE play_shares SET snapshot = ? WHERE id = ?'),
            (_json_dumps(snapshot), share_id))
