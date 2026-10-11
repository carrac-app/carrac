"""MCP tools: chart screenshot + volume profile (2 tools).

  chart_get_screenshot  — return base64-encoded JPEG of a chart screenshot
  chart_get_volume_profile    — compute volume profile for a symbol/timeframe window
"""

from carrac.site_url import site_base_url
import base64
import logging
import os
import time

import pandas as pd

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id  # noqa: F401 — available if needed

logger = logging.getLogger('carrac.mcp.charts')

_DEFAULT_INDICATORS = 'CPR,EMA_RIBBON,WEEKLY_MA,DAILY_EMA'

# Agent-sane screenshot defaults. A 1280x720 quality-70 render produces a ~123,700-char base64 payload that blew past the MCP
# tool-response size limit on the very first ordinary call — the image is
# meant for a human/multimodal viewer to look at, not for the calling agent
# to receive as text, so the default render is kept small. Callers that
# actually need high-resolution detail can still pass larger width/height/
# quality explicitly (up to the existing 3840 cap).
_DEFAULT_SCREENSHOT_WIDTH = 640
_DEFAULT_SCREENSHOT_HEIGHT = 360
_DEFAULT_SCREENSHOT_QUALITY = 55
# Hard ceiling on the returned base64 payload. If a caller requests large
# enough dimensions/quality to blow past this, fail with an actionable error
# instead of silently truncating (which would corrupt the JPEG) or letting
# an oversized payload break the calling agent's loop.
_MAX_SCREENSHOT_B64_CHARS = 100_000

# Timeframe → source interval for volume profile granularity (mirrors routes.py)
_VP_SOURCE_INTERVAL = {
    '1M': '1d', '1W': '4h', '3D': '1h',
    '1D': '15m', '4h': '5m', '1h': '1m',
}


# =============================================================================
# chart_get_screenshot
# =============================================================================
@mcp_tool(
    'chart_get_screenshot',
    'Render a live Carrac chart to JPEG and return base64 + metadata. '
    'Returns: {image_base64, mime_type ("image/jpeg"), asset, timeframe, width, height, size_bytes, indicators, cached}. '
    'IMPORTANT: image_base64 is for a human or multimodal viewer to look at — it is '
    'NOT meant for the calling agent to read as text, and large renders can be too big '
    'for the calling agent to receive at all. Defaults are intentionally small '
    f'({_DEFAULT_SCREENSHOT_WIDTH}x{_DEFAULT_SCREENSHOT_HEIGHT}, quality {_DEFAULT_SCREENSHOT_QUALITY}) to stay '
    'well within typical MCP tool-response size limits; only raise width/height/quality when a '
    'human explicitly needs higher-resolution detail. The response is never silently truncated — '
    'if the rendered image is still too large, the call fails with an error asking you to lower '
    'width/height/quality instead of returning corrupted image data. '
    'Disk-cached for 1 hour — repeated calls for the same asset+timeframe+size are instant. '
    'First render ~5–60s (Playwright headless). Requires SCREENSHOT_API_KEY env var and the Carrac app on localhost:5000. '
    'Default indicators: CPR,EMA_RIBBON,WEEKLY_MA,DAILY_EMA. Supported timeframes: 1D, 4h, 1h, 1W, 3D, 1M. '
    'Use when a human or multimodal model needs to visually inspect price action. '
    'Not needed for data extraction — use market_get_chart or chart_get_volume_profile instead. '
    'min_tier: member.',
    {
        'type': 'object',
        'properties': {
            'asset': {
                'type': 'string',
                'description': 'Asset symbol, e.g. BTC, ETH',
            },
            'timeframe': {
                'type': 'string',
                'description': 'Chart timeframe, e.g. 1D, 4h, 1h, 1W (default 1D)',
            },
            'width': {
                'type': 'integer',
                'description': (
                    f'Screenshot width in pixels (default {_DEFAULT_SCREENSHOT_WIDTH}, max 3840). '
                    'Kept small by default — the image is for a human/multimodal viewer, not for '
                    'the calling agent to parse. Only increase if a human needs more detail.'
                ),
            },
            'height': {
                'type': 'integer',
                'description': (
                    f'Screenshot height in pixels (default {_DEFAULT_SCREENSHOT_HEIGHT}, max 3840).'
                ),
            },
            'quality': {
                'type': 'integer',
                'description': f'JPEG quality 1–100 (default {_DEFAULT_SCREENSHOT_QUALITY})',
            },
            'indicators': {
                'type': 'string',
                'description': (
                    f'Comma-separated indicator keys (default: {_DEFAULT_INDICATORS})'
                ),
            },
        },
        'required': ['asset'],
    },
    min_tier='member',
)
def chart_get_screenshot(arguments):
    import re
    import requests

    asset = (arguments.get('asset') or '').strip().upper()
    timeframe = (arguments.get('timeframe') or '1D').strip().upper()
    width = min(max(int(arguments.get('width') or _DEFAULT_SCREENSHOT_WIDTH), 100), 3840)
    height = min(max(int(arguments.get('height') or _DEFAULT_SCREENSHOT_HEIGHT), 100), 3840)
    quality = min(max(int(arguments.get('quality') or _DEFAULT_SCREENSHOT_QUALITY), 1), 100)
    indicators = (arguments.get('indicators') or _DEFAULT_INDICATORS).strip()

    if not asset:
        raise ToolError('asset is required')

    # Input validation (mirrors the Flask route's safety checks)
    _SAFE_ASSET_RE = re.compile(r'^[A-Z0-9_\-\.]{1,20}$')
    _SAFE_TF_RE = re.compile(r'^[A-Z0-9]{1,5}$')
    _SAFE_INDICATORS_RE = re.compile(r'^[A-Z0-9_,]{1,200}$')

    if not _SAFE_ASSET_RE.match(asset):
        raise ToolError(f'Invalid asset symbol: {asset}')
    if not _SAFE_TF_RE.match(timeframe):
        raise ToolError(f'Invalid timeframe: {timeframe}')
    if indicators and not _SAFE_INDICATORS_RE.match(indicators):
        raise ToolError('Invalid indicators parameter')

    # Check disk cache first (same path as Flask route)
    cache_dir = os.path.join('data', 'screenshots')
    cache_file = os.path.join(cache_dir, f'{asset}_{timeframe}_{width}x{height}.jpg')
    cached = False
    try:
        if os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < 3600:
                with open(cache_file, 'rb') as f:
                    img_bytes = f.read()
                cached = True
    except Exception:
        img_bytes = None

    if not cached:
        # Forward to the keyed endpoint via localhost (same-process HTTP loopback)
        api_key = os.getenv('SCREENSHOT_API_KEY', '')
        if not api_key:
            raise ToolError(
                'Screenshot capture is not configured on this server — '
                'the operator needs to set SCREENSHOT_API_KEY.'
            )

        try:
            resp = requests.get(
                f'http://localhost:5000/api/screenshot/{asset}/{timeframe}',
                params={
                    'width': width,
                    'height': height,
                    'quality': quality,
                    'indicators': indicators,
                },
                headers={'X-Internal-Key': api_key},
                timeout=60,
            )
        except requests.exceptions.ConnectionError:
            raise ToolError(
                'Could not connect to localhost:5000 for screenshot generation. '
                'Is the Carrac app running?'
            )
        except requests.exceptions.Timeout:
            raise ToolError('Screenshot generation timed out (>60s)')
        except Exception as exc:
            raise ToolError(f'Screenshot request failed: {exc}')

        if resp.status_code == 401:
            raise ToolError('Screenshot API key rejected (401). Check SCREENSHOT_API_KEY.')
        if resp.status_code == 503:
            # concurrency-cap busy response — surface it as an
            # actionable retry hint rather than a bare HTTP status.
            retry_after = resp.headers.get('Retry-After', '30')
            raise ToolError(
                f'Screenshot service is busy (too many renders in flight). '
                f'Retry in ~{retry_after}s.'
            )
        if resp.status_code != 200:
            raise ToolError(
                f'Screenshot endpoint returned HTTP {resp.status_code}'
            )
        img_bytes = resp.content

    if not img_bytes:
        raise ToolError('Screenshot returned empty response')

    image_b64 = base64.b64encode(img_bytes).decode('ascii')
    if len(image_b64) > _MAX_SCREENSHOT_B64_CHARS:
        # Refuse rather than silently truncate — a truncated base64 payload
        # would decode to a corrupted JPEG. This also protects the calling
        # agent's loop from an oversized tool response even when explicit
        # (larger-than-default) width/height/quality were requested.
        raise ToolError(
            f'Rendered screenshot is too large to return '
            f'({len(image_b64)} base64 chars, limit {_MAX_SCREENSHOT_B64_CHARS}). '
            f'Retry with smaller width/height and/or lower quality — this image is '
            f'meant for a human/multimodal viewer, not for the calling agent to '
            f'receive as text. Current request: {width}x{height} @ quality {quality}.'
        )

    return {
        'asset': asset,
        'timeframe': timeframe,
        'image_base64': image_b64,
        'mime_type': 'image/jpeg',
        'width': width,
        'height': height,
        'size_bytes': len(img_bytes),
        'indicators': indicators,
        'cached': cached,
    }


# =============================================================================
# chart_get_volume_profile
# =============================================================================
@mcp_tool(
    'chart_get_volume_profile',
    'Compute a volume profile (VP) for a symbol over a configurable time window. '
    'Returns: bins [{price (bin centre), buy, sell, total}], '
    'poc (Point of Control price), vah (Value Area High), val (Value Area Low), '
    'symbol, timeframe, source_interval, bar_count, num_bins, period_iso {requested/actual start+end}. '
    'period_iso.requested_start/end always reflect what you asked for. actual_start/end reflect the '
    'real first/last bar timestamp when available; for some symbols the underlying data fetch does not '
    'return per-bar timestamps, in which case actual_start/end are null and period_iso.note explains why '
    '— use requested_start/end as the window bounds in that case. '
    'Specify window via start+end (ms epoch), from_date+to_date (ISO), or lookback_days (default 30). '
    'Timeframe controls data granularity: 1D→15m bars, 4h→5m, 1h→1m, 1W→4h, 3D→1h, 1M→1d. '
    'Use to identify high-volume nodes, liquidity clusters, and S/R zones. '
    'Lighter alternative for zone lookup: scanner_get_confluence_zones (no computation, from scanner cache). '
    'min_tier: member.',
    {
        'type': 'object',
        'properties': {
            'symbol': {
                'type': 'string',
                'description': 'Asset symbol, e.g. BTC, ETH',
            },
            'timeframe': {
                'type': 'string',
                'description': (
                    'Chart timeframe for which to build the VP '
                    '(determines granularity of underlying data). '
                    'Supported: 1D, 4h, 1h, 1W, 3D, 1M. Default: 1D.'
                ),
            },
            'start': {
                'type': 'integer',
                'description': 'Start of VP window as ms epoch (overrides from_date)',
            },
            'end': {
                'type': 'integer',
                'description': 'End of VP window as ms epoch (default: now)',
            },
            'from_date': {
                'type': 'string',
                'description': (
                    'Start date as ISO string (e.g. "2026-01-01"). '
                    'Ignored if start (ms) is provided.'
                ),
            },
            'to_date': {
                'type': 'string',
                'description': (
                    'End date as ISO string. Ignored if end (ms) is provided. '
                    'Defaults to now.'
                ),
            },
            'lookback_days': {
                'type': 'integer',
                'description': (
                    'Alternative to start/from_date: lookback N days from now '
                    '(default 30). Ignored if start or from_date is supplied.'
                ),
            },
            'num_bins': {
                'type': 'integer',
                'description': 'Number of price bins (default 200, range 20–500)',
            },
        },
        'required': ['symbol'],
    },
    min_tier='member',
)
def chart_get_volume_profile(arguments):
    from carrac.services import get_services
    from volume_profile import compute_volume_profile, TF_SOURCE_MAP

    symbol = (arguments.get('symbol') or '').strip().upper()
    timeframe = (arguments.get('timeframe') or '1D').strip()
    num_bins = min(max(int(arguments.get('num_bins') or 200), 20), 500)

    if not symbol:
        raise ToolError('symbol is required')

    db = get_services().db

    # ── Resolve time window ──────────────────────────────────────────────────
    start_ms = arguments.get('start')
    end_ms = arguments.get('end')

    if start_ms is None:
        from_date = arguments.get('from_date')
        if from_date:
            try:
                start_ts = pd.Timestamp(from_date, tz='UTC')
                start_ms = int(start_ts.timestamp() * 1000)
            except Exception:
                raise ToolError(f'Cannot parse from_date="{from_date}"')
        else:
            lookback = int(arguments.get('lookback_days') or 30)
            start_ms = int((pd.Timestamp.now('UTC') - pd.Timedelta(days=lookback)).timestamp() * 1000)

    if end_ms is None:
        to_date = arguments.get('to_date')
        if to_date:
            try:
                end_ts = pd.Timestamp(to_date, tz='UTC')
                end_ms = int(end_ts.timestamp() * 1000)
            except Exception:
                raise ToolError(f'Cannot parse to_date="{to_date}"')
        else:
            end_ms = int(pd.Timestamp.now('UTC').timestamp() * 1000)

    start_ms = int(start_ms)
    end_ms = int(end_ms)

    if start_ms >= end_ms:
        raise ToolError('start must be before end')

    # Determine source interval from TF_SOURCE_MAP
    source_interval = TF_SOURCE_MAP.get(timeframe)
    if not source_interval:
        # Fall back: use the timeframe directly as the source interval
        source_interval = timeframe.lower()
        logger.warning(
            'Timeframe %s not in TF_SOURCE_MAP — using %s as source interval',
            timeframe, source_interval
        )

    # Fetch data for the window
    try:
        df = db.fetch_klines_for_range(symbol, source_interval, start_ms, end_ms)
    except Exception as exc:
        raise ToolError(f'Failed to fetch kline data: {exc}')

    if df is None or df.empty:
        raise ToolError(
            f'No kline data found for {symbol}/{timeframe} in the requested window. '
            f'Try a shorter lookback or a higher timeframe.'
        )

    # Coerce index to tz-aware UTC DatetimeIndex.
    #
    # Some venue paths (notably fetch_klines_for_range) return a bare positional
    # RangeIndex instead of per-bar timestamps. Treating that as ms-epoch would
    # yield ~1970 "actual_start"/"actual_end" values. Real bar timestamps are
    # always after 2015-01-01, so an earlier coerced date means the index was
    # never timestamps and actual_start/actual_end must not be reported.
    index_has_real_timestamps = True
    if not pd.api.types.is_datetime64_any_dtype(df.index):
        coerced = pd.to_datetime(df.index, unit='ms', utc=True)
        if len(coerced) and coerced[0] < pd.Timestamp('2015-01-01', tz='UTC'):
            index_has_real_timestamps = False
        else:
            df.index = coerced
    elif df.index.tz is None:
        df.index = df.index.tz_localize('UTC')

    # Compute volume profile
    try:
        vp = compute_volume_profile(df, num_bins=num_bins)
    except Exception as exc:
        raise ToolError(f'Volume profile computation failed: {exc}')

    if vp is None:
        raise ToolError(
            f'Volume profile could not be computed for {symbol}/{timeframe} — '
            f'insufficient data ({len(df)} bars).'
        )

    # Attach period metadata
    period_iso = {
        'requested_start': pd.Timestamp(start_ms, unit='ms', tz='UTC').isoformat(),
        'requested_end': pd.Timestamp(end_ms, unit='ms', tz='UTC').isoformat(),
        'actual_start': None,
        'actual_end': None,
    }
    if index_has_real_timestamps and not df.empty:
        period_iso['actual_start'] = df.index[0].isoformat()
        period_iso['actual_end'] = df.index[-1].isoformat()
    else:
        period_iso['note'] = (
            'actual_start/actual_end are unavailable: the underlying kline '
            'fetch for this symbol/timeframe did not return per-bar '
            'timestamps. Use requested_start/requested_end as the window '
            'bounds — the bars are known to fall within that window, just '
            'not individually timestamped here.'
        )

    return {
        **vp,
        'symbol': symbol,
        'timeframe': timeframe,
        'source_interval': source_interval,
        'bar_count': len(df),
        'num_bins': num_bins,
        'period_iso': period_iso,
    }


# No longer a standalone MCP tool — absorbed into share_manage
# (entity_type=chart, action=share). Kept as a plain helper, called from
# carrac.mcp.tools.sharing. The legacy 'share_chart' tool name still
# dispatches via registry.LEGACY_TOOL_ALIASES + LEGACY_ARG_DEFAULTS.
#
# Mints a PUBLIC, frozen-snapshot share link for a chart (the symbol +
# timeframe passed in). Anyone can open the returned URL with no Carrac
# account: the og:image card is a full-indicator server-side render (CPR,
# EMA Ribbon, Weekly MA, Daily EMA — captured NOW via a headless render of
# the live chart), automatically falling back to a plain candlestick-only
# card (last ~120 bars) if the render service is disabled or unavailable —
# this call never fails because of that. The page also shows a frozen
# per-asset context (nearest confluence zone, scanner verdict, market
# regime) plus a "view live" funnel to Carrac. Each call mints a NEW link (a
# chart share is a moment in time) and counts against the per-tier
# active-share quota (shared pool with play + trade shares). Revoke via
# share_manage(entity_type=chart, action=unshare, slug=...).
def share_chart(arguments):

    from carrac.mcp.runtime import get_mcp_caller_tier
    from carrac.plays.shares import (
        count_active_shares,
        create_chart_share,
        share_quota,
    )

    user_id = get_mcp_user_id()
    symbol = (arguments.get('symbol') or '').strip()
    if not symbol:
        raise ToolError('symbol is required')
    timeframe = (arguments.get('timeframe') or '1D').strip()

    # Every chart share is a new mint → charge quota up front.
    tier = get_mcp_caller_tier()
    quota = share_quota(tier)
    current = count_active_shares(user_id)
    if current >= quota:
        raise ToolError(
            f'Share limit reached ({current}/{quota} active links on the '
            f'{tier} tier). Revoke an old share or upgrade for more.')

    share = create_chart_share(user_id, symbol, timeframe)
    if not share:
        raise ToolError(f'No chart data for {symbol} {timeframe}')

    base = site_base_url()
    snap = share.get('snapshot') or {}
    return {
        'slug': share['slug'],
        'url': f"{base}/s/{share['slug']}",
        'image_url': f"{base}/s/{share['slug']}/card.png",
        'symbol': snap.get('symbol'),
        'timeframe': snap.get('timeframe'),
        'change_pct': snap.get('change_pct'),
        'view_count': share.get('view_count', 0),
    }


# No longer a standalone MCP tool — absorbed into share_manage
# (entity_type=chart, action=unshare). Revokes a chart share link by its
# slug (the value share_chart returned). SIDE EFFECTS: the public URL
# immediately returns 410 Gone; the freed slot returns to the per-tier
# active-share quota.
def unshare_chart(arguments):
    from carrac.plays.shares import revoke_share_by_slug

    slug = (arguments.get('slug') or '').strip()
    if not slug:
        raise ToolError('slug is required')

    revoked = revoke_share_by_slug(slug, get_mcp_user_id())
    return {'slug': slug, 'revoked': revoked}
