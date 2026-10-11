"""Pydantic validation errors flip from 422 to 400.

The production app's ``validation_exception_handler`` is registered via
``exception_handlers.install``. This changes the status code so the
response shape matches Flask's existing 400 + ``{"error": "..."}`` body.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Query
from httpx import ASGITransport, AsyncClient

from carrac.fastapi_app import exception_handlers


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_validation_error_returns_400_with_canonical_shape():
    """Type mismatch on a required Query param → 400 with both ``error`` and ``errors``."""

    async def _go():
        app = FastAPI()
        exception_handlers.install(app)

        @app.get("/test-validate")
        async def _h(count: int = Query(...)):
            return {"count": count}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/test-validate?count=notanint")

    r = _run(_go())
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
    body = r.json()
    assert "error" in body
    assert isinstance(body["error"], str)
    # ``errors`` array preserved for clients that introspect.
    assert "errors" in body
    assert isinstance(body["errors"], list)
