"""The Flask bridge must not borrow another request's executor.

Root cause, confirmed by reproduction:

``asgiref.wsgi.WsgiToAsgiInstance.run_wsgi_app`` is decorated with a bare
``@sync_to_async``, i.e. ``thread_sensitive=True``.  That makes asgiref pick
the executor from shared state rather than a fixed pool
(``asgiref/sync.py``, ``SyncToAsync.__call__``)::

    current_thread_executor = getattr(AsyncToSync.executors, "current", None)
    if current_thread_executor:
        executor = current_thread_executor      # <-- borrowed

Every in-flight WSGI request sets that attribute, because
``WsgiToAsgiInstance.__call__`` builds ``self.sync_send = AsyncToSync(send)``
and ``AsyncToSync.__call__`` publishes its ``CurrentThreadExecutor`` onto the
class-level ``AsyncToSync.executors`` Local for the duration of the call.

Under concurrency, request B therefore submits its WSGI work onto request A's
executor.  When A finishes, that executor quits, and B (or a later request)
dies with::

    RuntimeError: CurrentThreadExecutor already quit or is broken

which surfaces as a 500 on whatever Flask route happened to be next — in
production, ``GET /login``, because every login-gated page redirects there and
the redirect follow arrives while the first request is still in flight.

The fix pins the bridge to a dedicated single-thread executor
(``carrac.asgi.FLASK_WSGI_EXECUTOR``) with ``thread_sensitive=False``, so the
executor can never be borrowed from — or by — another request.  ``max_workers``
stays at 1 to preserve the existing serialisation semantics exactly; see
``carrac/asgi.py`` and ``fastapi_app/routers/chart_public.py`` for why Flask
concurrency here is deliberately capped.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from asgiref.current_thread_executor import CurrentThreadExecutor
from asgiref.sync import AsyncToSync
from asgiref.wsgi import WsgiToAsgi
from flask import Flask
from httpx import ASGITransport

from carrac.asgi import FLASK_WSGI_EXECUTOR, IsolatedWsgiToAsgi


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tiny_wsgi_app() -> Flask:
    """A Flask app with no Carrac wiring — this is a bridge test, not an app test."""
    app = Flask(__name__)

    @app.get("/ping")
    def ping():  # pragma: no cover - trivial
        return "pong"

    return app


def _quit_sibling_executor() -> CurrentThreadExecutor:
    """Build a CurrentThreadExecutor that has already quit, on another thread.

    Faithful to production: the executor that gets borrowed belongs to a
    *different* request and was created on that request's Flask worker thread.
    Building it on this thread instead would trip asgiref's separate
    "cannot submit onto CurrentThreadExecutor from its own thread" guard and
    the test would pass for the wrong reason.
    """
    holder: dict[str, CurrentThreadExecutor] = {}

    def build() -> None:
        executor = CurrentThreadExecutor(None)
        # What asgiref does when the owning AsyncToSync call finishes.
        executor._broken = True
        holder["executor"] = executor

    thread = threading.Thread(target=build)
    thread.start()
    thread.join()
    return holder["executor"]


def _get_through_bridge(bridge, path: str = "/ping") -> httpx.Response:
    """Drive one request through an ASGI app, poisoning the Local first.

    The poison is set *inside* the loop's context so the read in
    ``SyncToAsync.__call__`` sees it, exactly as a concurrent sibling request
    would have left it.
    """

    async def _run() -> httpx.Response:
        AsyncToSync.executors.current = _quit_sibling_executor()
        transport = ASGITransport(app=bridge)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(path)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()
        # Don't leak the poison into other tests in this process.
        try:
            del AsyncToSync.executors.current
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBorrowedExecutorRegression:
    """The stock asgiref bridge fails on a borrowed executor; the isolated bridge does not."""

    def test_stock_asgiref_bridge_still_fails(self):
        """Show it failing — proves the reproduction is real, not decorative.

        If asgiref ever fixes this upstream this test will fail, which is the
        signal to drop ``IsolatedWsgiToAsgi`` rather than carry it forever.
        """
        bridge = WsgiToAsgi(_tiny_wsgi_app())

        with pytest.raises(RuntimeError, match="already quit or is broken"):
            _get_through_bridge(bridge)

    def test_isolated_bridge_survives_a_dead_borrowed_executor(self):
        """The fix: a poisoned Local cannot reach our bridge's executor."""
        bridge = IsolatedWsgiToAsgi(_tiny_wsgi_app())

        response = _get_through_bridge(bridge)

        assert response.status_code == 200
        assert response.text == "pong"


class TestBridgeConfiguration:
    """Guard the two properties the fix depends on."""

    def test_bridge_is_not_thread_sensitive(self):
        """thread_sensitive=True is what reaches for shared executor state.

        Re-imports from carrac.asgi rather than using this module's top-level
        binding: tests/fastapi/test_screenshot_public.py and test_chart_public.py
        importlib.reload(carrac.asgi) to exercise their env-var branches, which
        rebinds FLASK_WSGI_EXECUTOR and the IsolatedWsgiToAsgi *class object*.
        A stale module-level reference then fails an `is`/isinstance check even
        though the live app is configured correctly — a false alarm, and this
        guard is about the app's current wiring, not import order.
        """
        from carrac.asgi import (FLASK_WSGI_EXECUTOR as current_executor,
                                 _IsolatedWsgiToAsgiInstance)

        # __dict__ access: attribute access returns a bound functools.partial.
        run_wsgi_app = _IsolatedWsgiToAsgiInstance.__dict__["run_wsgi_app"]

        assert run_wsgi_app._thread_sensitive is False
        assert run_wsgi_app._executor is current_executor

    def test_serialisation_semantics_are_preserved(self):
        """One Flask request at a time per worker — unchanged from asgiref's
        ``single_thread_executor``.  Raising this is a real behaviour change, not a free win, so it is pinned here deliberately.
        """
        assert FLASK_WSGI_EXECUTOR._max_workers == 1

    def test_app_uses_the_isolated_bridge(self):
        """What calls this — the wired app must actually use the fixed bridge.

        Both names come from the module together, for the reload reason in
        test_bridge_is_not_thread_sensitive: pairing a freshly-read instance
        with a stale class object is what made this fail in a full-suite run
        while passing in isolation.
        """
        from carrac.asgi import (IsolatedWsgiToAsgi as CurrentBridge,
                                 flask_asgi)

        assert isinstance(flask_asgi, CurrentBridge)
