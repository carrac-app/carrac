"""Cache-Control middleware for the FastAPI sub-app.

Ports the Flask ``after_request`` hook in ``carrac/__init__.py`` (which calls
``carrac/cache_headers.py::apply_cache_headers``) to a raw ASGI
middleware. The Flask handler stays in place — both runtimes apply the
same policy table independently for the routes they serve.

Behaviour mirrors the Flask API branch only: the FastAPI sub-app never
serves ``/static/``, never returns ``text/html`` (those stay on Flask),
so the static/HTML branches don't fire here. ``/api/chart/`` and
``/api/chart-internal/`` are skipped — those handlers manage their own
``Cache-Control``.

Idempotent: skips paths whose handler already set a ``Cache-Control``
header explicitly (the same pattern as ``ResponseHeadersMiddleware``).
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class CacheHeadersMiddleware:
    """Inject ``Cache-Control`` on http.response.start via the policy table."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET")
        # Chart and chart-internal handlers manage their own Cache-Control.
        skip = (
            path.startswith("/api/chart/")
            or path.startswith("/api/chart-internal/")
        )
        # The Flask after_request hook only runs the API branch for these
        # path families. Same gate here so we don't accidentally tag a
        # FastAPI-served path that wasn't covered by the Flask side.
        applies = (
            path.startswith("/api/")
            or path.startswith("/poll")
            or path.startswith("/widget/")
        )

        async def send_with_cache(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and not skip
                and applies
            ):
                status: int = message.get("status", 200)
                headers = list(message.get("headers", []))
                existing = {k.lower() for (k, _) in headers}
                if b"cache-control" not in existing:
                    # Non-2xx responses must never be stored in a shared cache.
                    # This prevents nginx from caching a 401 (returned before
                    # the caller's API key was validated) and serving it to
                    # subsequent authenticated requests.
                    if not (200 <= status < 300):
                        headers.append((b"cache-control", b"no-store"))
                        if b"pragma" not in existing:
                            headers.append((b"pragma", b"no-cache"))
                        if b"expires" not in existing:
                            headers.append((b"expires", b"0"))
                    else:
                        # Late import to avoid loading the policy table at
                        # ASGI app startup — unit tests instantiate this
                        # middleware in isolation and the import is cheap.
                        from carrac.cache_headers import get_api_cache_header

                        header = get_api_cache_header(path, method)
                        headers.append((b"cache-control", header.encode("ascii")))
                        if "no-cache" in header:
                            if b"pragma" not in existing:
                                headers.append((b"pragma", b"no-cache"))
                            if b"expires" not in existing:
                                headers.append((b"expires", b"0"))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cache)
