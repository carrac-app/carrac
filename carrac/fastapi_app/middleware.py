"""HTTP middlewares for the FastAPI sub-app.

``ResponseHeadersMiddleware`` mirrors the Flask
``after_request`` set (``security.py::add_security_headers`` + the
``_add_api_version_header`` hook in ``carrac/__init__.py``) so
paths flipped from Flask to FastAPI via the strangler mount keep the
same prod response-header surface.

Tests in ``tests/test_app.py::TestSecurityHeaders`` and
``tests/test_openapi.py::test_version_header_on_health`` still hit
Flask directly via the test client because the Flask handler was kept
for a short parity period; this middleware covers the strangler-routed
prod traffic path.
"""

from __future__ import annotations

from carrac.csp import build_csp
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _api_version() -> bytes:
    # Late import: ``carrac.openapi`` requires the Flask app context
    # to import cleanly in some test paths. Cached after first call.
    global _CACHED_API_VERSION
    if _CACHED_API_VERSION is None:
        try:
            from carrac.openapi import API_VERSION

            _CACHED_API_VERSION = str(API_VERSION).encode("ascii")
        except Exception:  # noqa: BLE001 — tolerate test-env import order
            _CACHED_API_VERSION = b"1"
    return _CACHED_API_VERSION


_CACHED_API_VERSION: bytes | None = None

# Content-Security-Policy for ASGI-served responses. Every response gets the
# strict policy (``carrac.csp.build_csp``, the same one Flask emits) except the
# headless chart page rendered for Playwright screenshots: it is replayed
# without a request nonce, so its inline scripts need ``'unsafe-inline'``, and
# the driver's ``page.wait_for_function()`` needs ``'unsafe-eval'``.
_CSP = build_csp().encode("ascii")
_HEADLESS_CSP = build_csp(allow_inline=True, allow_eval=True).encode("ascii")
_HEADLESS_PREFIX = "/chart-headless/"


class ResponseHeadersMiddleware:
    """ASGI middleware injecting the standard Flask after_request headers.

    Idempotent: skips any header a handler already set explicitly.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        csp = _HEADLESS_CSP if scope["path"].startswith(_HEADLESS_PREFIX) else _CSP

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.lower() for (k, _) in headers}
                additions: list[tuple[bytes, bytes]] = [
                    (b"content-security-policy", csp),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (
                        b"permissions-policy",
                        b"geolocation=(), microphone=(), camera=()",
                    ),
                    (b"x-api-version", _api_version()),
                    # Always emit HSTS with ``preload`` to match Flask
                    # parity. Prod is HTTPS-only via Cloudflare/nginx so
                    # the directive is meaningful; if a request ever
                    # arrived over plain HTTP browsers ignore HSTS
                    # anyway. A config-driven gate on one side and an
                    # env-driven gate on the other can diverge silently,
                    # so there is no gate.
                    (
                        b"strict-transport-security",
                        b"max-age=31536000; includeSubDomains; preload",
                    ),
                ]
                for name, value in additions:
                    if name not in existing:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
