"""A raw WebSocket to the Flask fallthrough must not 500.

Internet scanners open ``ws://<server-ip>/`` against the bare host (no path
Carrac ever advertises). Starlette's strangler router (``carrac.asgi.asgi_app``)
has no WebSocket route anywhere to match that scope against — checked
``carrac/`` and ``carrac/fastapi_app/`` for ``WebSocketRoute``,
``@app.websocket``, or any Starlette websocket route: none exist. Every
"websocket" symbol in the codebase is an outbound WS *client* (Binance,
Hyperliquid) used by background workers, never an endpoint we serve to a
browser or a probe. So a websocket scope always falls through every
``ExactPath``/``PrefixPath``/``CurlChartPath`` entry (each of those already
guards ``scope["type"] != "http"``) to the terminal ``Mount("/",
app=flask_asgi)``.

``flask_asgi`` is ``carrac.asgi.IsolatedWsgiToAsgi``, which historically
handed every scope straight to asgiref's ``WsgiToAsgiInstance.__call__``.
That method raises ``ValueError("WSGI wrapper received a non-HTTP scope")``
on anything but ``scope["type"] == "http"`` — Starlette has no handler for
that, so it propagates as an unhandled exception and the error tracker records one
event per scan (7/week on release 0.226.16).

The fix (``carrac/asgi.py``, ``IsolatedWsgiToAsgi.__call__``) intercepts a
non-HTTP scope before it ever reaches asgiref: a ``"websocket"`` scope gets
a clean ``websocket.close`` (code 1000 — the same default Starlette's own
``WebSocketClose`` helper uses when a websocket scope matches no declared
route, since that is exactly the situation here); anything else is ignored
quietly.
"""

from __future__ import annotations

import pytest
from asgiref.wsgi import WsgiToAsgi
from flask import Flask
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from carrac.asgi import IsolatedWsgiToAsgi


def _tiny_wsgi_app() -> Flask:
    """A Flask app with no Carrac wiring — this is a bridge test, not an app test."""
    app = Flask(__name__)

    @app.get("/ping")
    def ping():  # pragma: no cover - trivial
        return "pong"

    return app


class TestBridgeRejectsWebsocketScopeWithoutRaising:
    """Websocket scope handling of the bridge, isolated from the rest of the app (no DB, no env)."""

    def test_stock_asgiref_bridge_still_raises_on_websocket_scope(self):
        """The stock bridge raises on a websocket scope; this is asgiref behaviour, not Carrac configuration.

        A plain asgiref ``WsgiToAsgi`` — what ``IsolatedWsgiToAsgi`` wraps —
        raises on a websocket scope. If asgiref ever starts handling this
        itself, this test fails, which is the signal our workaround in
        ``carrac.asgi.IsolatedWsgiToAsgi`` can be dropped.
        """
        bridge = WsgiToAsgi(_tiny_wsgi_app())
        client = TestClient(bridge)

        with pytest.raises(ValueError, match="non-HTTP scope"):
            with client.websocket_connect("/"):
                pass  # pragma: no cover - the connect itself raises

    def test_isolated_bridge_closes_websocket_scope_without_raising(self):
        """Connecting closes cleanly instead of raising ValueError.

        ``IsolatedWsgiToAsgi.__call__`` checks the scope type, so a websocket
        scope never reaches the stock bridge's
        ``ValueError("WSGI wrapper received a non-HTTP scope")``.
        """
        bridge = IsolatedWsgiToAsgi(_tiny_wsgi_app())
        client = TestClient(bridge)

        # The server sends websocket.close before ever accepting, so the
        # client observes the handshake being rejected — not a ValueError
        # bubbling out of the ASGI app.
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/"):
                pass  # pragma: no cover - rejected before accept

        assert exc_info.value.code == 1000


class TestProductionAsgiAppWebsocketFallthrough:
    """End-to-end through the real strangler dispatcher (the actual failing path)."""

    def test_websocket_to_bare_root_is_closed_not_500(self, asgi_app):
        """A scanner's ``ws://<ip>/`` must close cleanly, matching production wiring.

        ``asgi_app`` here is the same object wired at ``carrac.asgi.asgi_app``
        — FastAPI sub-app first, Flask ``Mount("/")`` last. "/" matches no
        FastAPI route for a websocket scope (``CurlChartPath.matches`` and
        every ``ExactPath``/``PrefixPath`` explicitly require
        ``scope["type"] == "http"``), so this exercises the exact fallthrough
        the error tracker caught scanners hitting.
        """
        client = TestClient(asgi_app)

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/"):
                pass  # pragma: no cover - rejected before accept

        assert exc_info.value.code == 1000
