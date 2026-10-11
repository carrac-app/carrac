"""Dashboard status endpoint — FastAPI port with an asyncio.gather rewrite.

* ``GET /api/status``  — health probe across 5 external data APIs.

The Flask handler runs the probes sequentially. Each probe has a 5 s
timeout, so a single slow upstream stretches the response to several
seconds; in the pathological case (all probes timing out) the total is
proportional to probe count. Under asyncio.gather over
``asyncio.to_thread`` calls, the wall time collapses to
``max(t_probe) ≈ 5 s``. Same external API surface, much better latency
under upstream weather.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import RateCtxDep, require_login
from carrac.fastapi_app.limiter import limiter

router = APIRouter()


# ---------------------------------------------------------------------------
# Probe helpers — each returns ``(name, status_dict)``.
# Functions are sync (use blocking ``requests``) so they run in a thread.
# ---------------------------------------------------------------------------


def _probe_stooq() -> tuple[str, dict]:
    import requests

    base = {
        "name": "Stooq",
        "desc": "Stocks, ETFs, Forex, Indices",
        "rate_limit": "Unlimited",
        "tier": "primary",
        "category": "TradFi",
    }
    try:
        resp = requests.get(
            "https://stooq.com/q/d/l/?s=aapl.us&i=d&d1=20250101&d2=20250102",
            timeout=5,
        )
        if resp.status_code == 200 and len(resp.text) > 50:
            return "stooq", {**base, "status": "ok"}
        return "stooq", {**base, "status": "error", "error": "Empty response"}
    except Exception as e:  # noqa: BLE001
        return "stooq", {**base, "status": "error", "error": str(e)[:50]}


def _probe_coinpaprika() -> tuple[str, dict]:
    import requests

    base = {
        "name": "Crypto Market Data",
        "desc": "Primary prices + market cap (TOTAL, BTC.D)",
        "rate_limit": "~2500/day",
        "tier": "primary",
        "category": "Prices",
    }
    try:
        resp = requests.get(
            "https://api.coinpaprika.com/v1/tickers/btc-bitcoin",
            timeout=5,
        )
        if resp.status_code == 200:
            return "crypto_market_data", {**base, "status": "ok"}
        if resp.status_code == 429:
            return (
                "crypto_market_data",
                {**base, "status": "ratelimit", "error": "Rate limited"},
            )
        return (
            "crypto_market_data",
            {**base, "status": "error", "error": f"HTTP {resp.status_code}"},
        )
    except Exception as e:  # noqa: BLE001
        return "crypto_market_data", {**base, "status": "error", "error": str(e)[:50]}


def _probe_binance() -> tuple[str, dict]:
    import requests

    base = {
        "name": "Binance",
        "desc": "24hr % change, funding rates",
        "rate_limit": "1200/min",
        "tier": "overlay",
        "category": "Prices",
    }
    try:
        resp = requests.get(
            "https://data-api.binance.vision/api/v3/ticker/24hr",
            params={"symbol": "BTCUSDT"},
            timeout=5,
        )
        if resp.status_code == 200:
            return "binance", {**base, "status": "ok"}
        if resp.status_code == 429:
            return "binance", {**base, "status": "ratelimit", "error": "Rate limited"}
        return (
            "binance",
            {**base, "status": "error", "error": f"HTTP {resp.status_code}"},
        )
    except Exception as e:  # noqa: BLE001
        return "binance", {**base, "status": "error", "error": str(e)[:50]}


def _probe_feargreed() -> tuple[str, dict]:
    import requests

    base = {
        "name": "Fear & Greed",
        "desc": "Sentiment index",
        "rate_limit": "300/min",
        "tier": "primary",
        "category": "Sentiment",
    }
    try:
        resp = requests.get("https://api.alternative.me/fng/", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if "data" in data:
                fg_value = data["data"][0].get("value", "?")
                fg_class = data["data"][0].get("value_classification", "")
                return (
                    "feargreed",
                    {
                        **base,
                        "status": "ok",
                        "desc": f"Sentiment (now: {fg_value} - {fg_class})",
                    },
                )
            return "feargreed", {**base, "status": "error", "error": "Invalid response"}
        return (
            "feargreed",
            {**base, "status": "error", "error": f"HTTP {resp.status_code}"},
        )
    except Exception as e:  # noqa: BLE001
        return "feargreed", {**base, "status": "error", "error": str(e)[:50]}


def _probe_crypto_metrics() -> tuple[str, dict]:
    base = {
        "name": "Crypto Metrics",
        "desc": "BTC.D, TOTAL etc",
        "rate_limit": "Cached",
        "tier": "derived",
        "category": "Metrics",
    }
    try:
        cache_path = Path(os.environ.get("DATA_DIR", "data")) / "market_caps_cache.json"
        if not cache_path.exists():
            return (
                "crypto_metrics",
                {
                    **base,
                    "status": "error",
                    "error": "No cache - rebuilds on next fetch",
                },
            )
        with open(cache_path, "r") as f:
            cache_data = json.load(f)
        meta = cache_data.get("_meta", {})
        cache_time = datetime.fromisoformat(meta.get("updated_at", "2000-01-01"))
        age_hours = (datetime.now() - cache_time).total_seconds() / 3600
        coin_count = meta.get("coin_count", 0)
        if age_hours < 6:
            return (
                "crypto_metrics",
                {
                    **base,
                    "status": "ok",
                    "desc": f"BTC.D, TOTAL ({coin_count} coins, {age_hours:.1f}h ago)",
                },
            )
        return (
            "crypto_metrics",
            {
                **base,
                "status": "stale",
                "error": f"Cache {age_hours:.0f}h old (TTL: 6h)",
            },
        )
    except Exception as e:  # noqa: BLE001
        return "crypto_metrics", {**base, "status": "error", "error": str(e)[:50]}


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/api/status")
@limiter.limit("12/minute")
async def api_status(
    request: Request,
    user: Annotated[dict, Depends(require_login)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Health probe across 5 external APIs — runs probes concurrently."""
    # No CoinCap probe: CoinCap retired its keyless v2 API (REST dead, WS
    # requires an API key), so the probe could only ever fail.
    # No CryptoCompare probe: it would itself be an outbound
    # call to a vendor barred on every commercial tier.
    probes = (
        _probe_stooq,
        _probe_coinpaprika,
        _probe_binance,
        _probe_feargreed,
        _probe_crypto_metrics,
    )

    pairs = await asyncio.gather(*(asyncio.to_thread(p) for p in probes))
    statuses: dict[str, Any] = {name: status for name, status in pairs}

    primary_ok = sum(
        1 for s in statuses.values() if s.get("tier") == "primary" and s["status"] == "ok"
    )
    primary_total = sum(1 for s in statuses.values() if s.get("tier") == "primary")
    fallback_ok = sum(
        1
        for s in statuses.values()
        if s.get("tier") in ("fallback", "overlay") and s["status"] == "ok"
    )
    fallback_total = sum(
        1 for s in statuses.values() if s.get("tier") in ("fallback", "overlay")
    )

    all_ok = all(s["status"] == "ok" for s in statuses.values())
    any_error = any(s["status"] == "error" for s in statuses.values())

    return JSONResponse({
        "overall": "ok" if all_ok else ("degraded" if not any_error else "error"),
        "apis": statuses,
        "summary": {
            "primary": f"{primary_ok}/{primary_total} online",
            "fallback": f"{fallback_ok}/{fallback_total} online",
        },
        "checked_at": datetime.now().isoformat(),
    })
