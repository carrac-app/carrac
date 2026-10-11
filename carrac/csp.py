"""The Content-Security-Policy, defined once for Flask and FastAPI.

``security.add_security_headers`` (Flask ``after_request``) and
``carrac.fastapi_app.middleware.ResponseHeadersMiddleware`` both call
``build_csp``; they differ only in the script-src switches. Hosts for PostHog,
Cloudflare Web Analytics and Paddle appear only when that feature is configured.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

# Third-party hosts every install needs, and the pages that load from them.
# jsdelivr and unpkg serve libraries the pages load at runtime, so they stay
# in the base policy.
_SCRIPT_HOSTS = (
    "https://cdnjs.cloudflare.com",  # three.js (landing), Sortable (app), html2pdf (morning report)
    "https://cdn.jsdelivr.net",  # echarts (journal, portfolio, intelligence), vanta (landing), marked (report view)
    "https://unpkg.com",  # swagger-ui bundle (/api/docs)
    "https://challenges.cloudflare.com",  # Turnstile (register)
    "blob:",
)

_CONNECT_HOSTS = (
    "'self'", "blob:", "wss://data-stream.binance.vision", "wss://ws.kraken.com",
    "wss://api.hyperliquid.xyz", "wss://stream.bybit.com", "wss://api.gateio.ws",
    "wss://ws.finnhub.io", "https://cdnjs.cloudflare.com",
    "https://challenges.cloudflare.com",
)

_FRAME_HOSTS = ("'self'", "https://challenges.cloudflare.com")

# Hosts of the optional integrations. Each is added only when its feature is
# switched on, so an install that never configures it never allows the host.
_POSTHOG_SCRIPT_HOSTS = (
    "https://us-assets.i.posthog.com",  # PostHog loader
    "https://eu-assets.i.posthog.com",
)
_POSTHOG_CONNECT_HOSTS = (
    "https://us.i.posthog.com", "https://eu.i.posthog.com",
    "https://us-assets.i.posthog.com", "https://eu-assets.i.posthog.com",
)
_CLOUDFLARE_INSIGHTS_HOST = "https://static.cloudflareinsights.com"  # the beacon Cloudflare injects
_PADDLE_HOST = "https://*.paddle.com"  # Paddle.js checkout


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _posthog_enabled() -> bool:
    """The client snippet renders only with POSTHOG_API_KEY."""
    return bool(os.getenv("POSTHOG_API_KEY"))


def _paddle_enabled() -> bool:
    """Checkout loads paddle.js only when both PADDLE_API_KEY and PADDLE_CLIENT_TOKEN are set."""
    return bool(os.getenv("PADDLE_API_KEY") and os.getenv("PADDLE_CLIENT_TOKEN"))


def _cloudflare_insights_enabled() -> bool:
    """Cloudflare injects its analytics beacon at the edge, which the app cannot
    see; set CLOUDFLARE_WEB_ANALYTICS=1 when Web Analytics is on for the zone."""
    return _truthy("CLOUDFLARE_WEB_ANALYTICS")


def _posthog_custom_origin() -> str | None:
    """Origin of a self-hosted PostHog (POSTHOG_HOST), when it is not the EU/US cloud."""
    host = os.getenv("POSTHOG_HOST", "").strip()
    parts = urlsplit(host)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    origin = f"{parts.scheme}://{parts.netloc}"
    return None if origin in _POSTHOG_CONNECT_HOSTS else origin


def build_csp(
    nonce: str | None = None,
    *,
    allow_inline: bool = False,
    allow_eval: bool = False,
) -> str:
    """Return the policy string.

    ``nonce`` authorises exactly the inline scripts carrying it. Without a
    nonce, scripts are limited to ``'self'`` plus the listed hosts unless
    ``allow_inline`` relaxes that to ``'unsafe-inline'``. ``allow_eval`` adds
    ``'unsafe-eval'`` (the headless chart page, driven by Playwright).
    """
    script = ["'self'"]
    if nonce:
        script.append(f"'nonce-{nonce}'")
    elif allow_inline:
        script.append("'unsafe-inline'")
    if allow_eval:
        script.append("'unsafe-eval'")
    script.extend(_SCRIPT_HOSTS)
    connect = list(_CONNECT_HOSTS)
    frame = list(_FRAME_HOSTS)
    if _posthog_enabled():
        script.extend(_POSTHOG_SCRIPT_HOSTS)
        connect.extend(_POSTHOG_CONNECT_HOSTS)
        custom = _posthog_custom_origin()
        if custom:
            script.append(custom)
            connect.append(custom)
    if _cloudflare_insights_enabled():
        script.append(_CLOUDFLARE_INSIGHTS_HOST)
    if _paddle_enabled():
        script.append(_PADDLE_HOST)
        connect.append(_PADDLE_HOST)
        frame.append(_PADDLE_HOST)
    return (
        "default-src 'self'; "
        f"script-src {' '.join(script)}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https: blob:; "
        f"frame-src {' '.join(frame)}; "
        f"connect-src {' '.join(connect)}; "
        "worker-src 'self' blob:; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "report-uri /api/csp-report;"
    )
