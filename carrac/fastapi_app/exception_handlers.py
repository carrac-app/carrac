"""Exception handlers normalising FastAPI errors to the Flask shape.

The canonical REST error JSON across the existing app is
``{"error": "<message>"}`` — a single string field, no ``"code"``.
FastAPI's defaults emit ``{"detail": ...}`` for HTTPException and a
structured list for 422 validation errors; both must be rewritten so
clients (which parse ``error`` today) keep working byte-compatibly.
"""

from __future__ import annotations

import re

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # Redirects (302/303/307/308) carry a ``Location`` header but no body.
    # The auth deps raise ``HTTPException(302, headers={"Location": ...})``
    # to mirror Flask's ``login_required``; returning a JSON ``error`` body
    # would confuse browsers that expect a redirect.
    if 300 <= exc.status_code < 400:
        return JSONResponse({}, status_code=exc.status_code, headers=exc.headers)

    detail = exc.detail
    # Track whether the caller provided a structured ``error`` field. Only
    # callers that raised ``HTTPException(detail={"error": ...})`` count —
    # bare-string details from slowapi etc. don't.
    caller_set_error = isinstance(detail, dict) and "error" in detail
    if isinstance(detail, dict):
        body: dict = detail if "error" in detail else {"error": str(detail)}
    elif isinstance(detail, str):
        body = {"error": detail}
    else:
        body = {"error": str(detail)}

    headers = dict(exc.headers or {})

    # Canonical 429 shape — matches ``carrac/rest_rate_limit.py``
    # ``{"error": "Rate limit exceeded", "retry_after": <int>, ...}`` plus a
    # ``Retry-After`` header. slowapi.errors.RateLimitExceeded
    # raises with a bare-string detail like ``"1 per 1 minute"`` — that would
    # leak through ``error`` field. Normalise the bare-string case to the
    # canonical message; preserve callers that explicitly set ``error`` in a
    # dict detail (widget-token + tier-gate paths).
    if exc.status_code == 429:
        if not caller_set_error:
            body["error"] = "rate limit exceeded"
        if "Retry-After" not in headers:
            if isinstance(body.get("retry_after"), int):
                headers["Retry-After"] = str(body["retry_after"])
            elif not caller_set_error:
                # slowapi doesn't always emit Retry-After; defensive default.
                headers["Retry-After"] = "60"
                body.setdefault("retry_after", 60)

    return JSONResponse(body, status_code=exc.status_code, headers=headers)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Flatten to a single, concise message. The full structured error
    # is preserved under ``errors`` for clients that opt in to debug it.
    # status_code is 400 (not FastAPI default 422) to match the
    # Flask-side shape clients have always parsed.
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        message = first.get("msg") or "invalid request"
        if loc:
            message = f"{loc}: {message}"
    else:
        message = "invalid request"
    return JSONResponse(
        {"error": message, "errors": jsonable_encoder(errors)}, status_code=400
    )


_CURL_PATH_RE = re.compile(r"^(/:[a-z]+|/curl-overview|/[A-Za-z0-9][A-Za-z0-9^.\-]{0,11}(?:@[A-Za-z0-9]{1,10})?)$")
_BROWSER_RE_429 = re.compile(r"Mozilla|Chrome|Safari|Edg", re.IGNORECASE)
_CURL_EXCLUDED_PATHS = frozenset({"/health", "/robots.txt", "/sitemap.xml", "/mcp", "/_ping"})


async def rate_limit_exception_handler(request: Request, exc):
    """429 handler: ANSI plain-text for curl users on curl-shaped paths; JSON for all others.

    Curl paths: /:help, /:assets, /curl-overview, and single-segment symbol paths
    (e.g. /btc, /btc@10d). Browsers and API consumers (non-curl UA or non-curl path)
    receive the existing canonical JSON 429 shape.

    Infrastructure paths (health checks, robots.txt, etc.) are explicitly excluded
    so they always receive JSON regardless of rate-limit rules added later.
    """
    path = request.url.path
    ua = request.headers.get("user-agent", "")
    is_browser = bool(_BROWSER_RE_429.search(ua))
    is_curl_path = bool(_CURL_PATH_RE.match(path))
    if path not in _CURL_EXCLUDED_PATHS and is_curl_path and not is_browser:
        # Lazy import to avoid circular: curl_charts.content → registry → ... nothing circular,
        # but the pattern is consistent with other lazy imports in this module boundary.
        from carrac.curl_charts.content import rate_limited
        from fastapi.responses import PlainTextResponse as _PR
        return _PR(rate_limited(plain=True), status_code=429, media_type="text/plain; charset=utf-8")
    return await http_exception_handler(request, exc)


def install(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    # slowapi raises ``RateLimitExceeded`` (subclass of HTTPException(429)).
    # Registering the same handler explicitly ensures the canonical 429 JSON shape
    # fires regardless of how FastAPI's exception dispatch resolves the type.
    # Curl endpoint: ``rate_limit_exception_handler`` wraps the JSON handler so
    # terminal users get an ANSI-friendly plain-text 429 instead of JSON.
    try:
        from slowapi.errors import RateLimitExceeded
    except ImportError:  # slowapi optional during tests / image rebuild
        return
    app.add_exception_handler(RateLimitExceeded, rate_limit_exception_handler)
