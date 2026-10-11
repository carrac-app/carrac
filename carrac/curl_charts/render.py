"""Render bridge: chart payload → carrac-render subprocess → Redis-cached ANSI."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os

from carrac.curl_charts.grammar import Bucket, Options

logger = logging.getLogger(__name__)

RENDER_BIN = os.environ.get("CURL_RENDER_BIN", os.path.join(os.getcwd(), "bin", "carrac-render"))
CACHE_TTL = 60
RENDER_TIMEOUT = 2.0
_sem = asyncio.Semaphore(2)


class RenderFailed(Exception):
    """Renderer crashed / timed out — maps to 503 'chart temporarily unavailable'."""


class DataUnavailable(Exception):
    """Upstream data gap — maps to 503 'data warming up'."""


def _redis():
    from carrac.redis_cache import get_redis

    return get_redis()


def _cache_key(symbol: str, bucket: Bucket, opts: Options) -> str:
    oh = hashlib.md5(
        f"{opts.ema}:{opts.plain}:{opts.quiet}:{opts.cols}".encode(),
        usedforsecurity=False,
    ).hexdigest()[:8]
    return f"curlrender:{symbol}:{bucket.label}:{oh}"


def _fetch_payload(symbol: str, bucket: Bucket) -> dict:
    """Sync — runs in a thread. Reuses the app's existing chart service."""
    from carrac.charts.service import build_chart_payload

    return build_chart_payload(symbol, timeframe=bucket.timeframe, days=bucket.bars)


async def _render_subprocess(payload: dict, bucket: Bucket, opts: Options) -> str:
    args = [
        RENDER_BIN, "render",
        "--cols", str(opts.cols),
        "--rows", "26",
        "--bars", str(bucket.bars),
        "--window-label", bucket.label,
    ]
    if opts.ema:
        args.append("--ema")
    if opts.plain:
        args.append("--plain")
    if opts.quiet:
        args.append("--quiet")

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:  # binary missing / not executable → friendly 503, not a 500
        raise RenderFailed(f"renderer spawn failed: {e}") from e
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(payload).encode()), timeout=RENDER_TIMEOUT
        )
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise RenderFailed("render timeout") from e
    if proc.returncode != 0:
        raise RenderFailed(f"render exit {proc.returncode}: {stderr.decode(errors='replace')[:200]}")
    return stdout.decode()


async def render_chart(symbol: str, bucket: Bucket, opts: Options) -> str:
    """Cached chart text for a canonical symbol + snapped bucket + options."""
    key = _cache_key(symbol, bucket, opts)
    r = None
    try:
        r = _redis()
    except Exception:
        r = None  # warned once below — both "raised" and "returned None" mean the same
    if r is None:
        logger.warning("curl render: redis unavailable, rendering uncached")
    if r is not None:
        try:
            cached = r.get(key)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else cached
        except Exception:
            logger.warning("curl render: redis get failed, rendering uncached", exc_info=True)

    async with _sem:
        try:
            payload = await asyncio.to_thread(_fetch_payload, symbol, bucket)
        except Exception as e:
            raise DataUnavailable(str(e)) from e
        text = await _render_subprocess(payload, bucket, opts)

    if r is not None:
        try:
            r.setex(key, CACHE_TTL, text.encode())
        except Exception:
            logger.warning("curl render: redis set failed", exc_info=True)
    return text
