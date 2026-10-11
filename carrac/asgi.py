"""Strangler-fig ASGI composition.

Routes /api/v2/* and the explicit migrated-path list to the FastAPI
sub-app; everything else falls through to the existing Flask app via
asgiref's WsgiToAsgi wrapper.

Per the FastAPI migration plan: new endpoints land in
FastAPI; flipping a route from Flask to FastAPI is an edit to the
``MIGRATED_PATHS`` / ``MIGRATED_WRITE_PATHS`` set below — no nginx, DNS,
or Dockerfile change required.

The FastAPI sub-app is mounted twice into the dispatcher:

* ``Mount("/api/v2", app=fastapi_app)`` — strips the prefix so the
  internal v2 routes (``/_ping`` etc.) live under ``/api/v2/`` from the
  outside while being declared at their own paths inside the FastAPI
  app.
* ``ExactPath(p, app=fastapi_app)`` for each ``p`` in
  ``MIGRATED_PATHS`` — does NOT strip, so the FastAPI app must declare
  these routes at their real, full path (e.g. ``@app.get("/health")``).
* ``MIGRATED_WRITE_PATHS`` and ``MIGRATED_WRITE_PREFIXES`` —
  same shape but with a per-entry method filter so a single resource
  path can be split across runtimes by HTTP method (``POST /api/plays``
  → FastAPI, ``GET /api/plays`` → Flask).

The double-mount is intentional: lifespan events fan out via the
``Mount`` route (Starlette's router forwards lifespan to mounted
sub-apps), while ``ExactPath`` is HTTP-only.

``MIGRATED_READ_PREFIXES`` is a method-scoped
``PrefixPath`` table (same shape as ``MIGRATED_WRITE_PREFIXES``) for a route
that flips to FastAPI as a thin "front door" while the Flask view underneath
stays completely unchanged: ``GET /api/chart/<symbol>`` resolves identity +
applies its rate limit on the FastAPI side (off asgiref's single-thread
executor, see ``asgiref.sync.SyncToAsync.single_thread_executor`` —
4 concurrent Flask requests system-wide), then replays the existing Flask
view via ``asyncio.to_thread`` (``fastapi_app/routers/chart_public.py``).
Gated by ``CHART_ASGI_ENABLED`` (default on) for an instant `.env` rollback.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import sync_to_async
from asgiref.wsgi import WsgiToAsgi, WsgiToAsgiInstance
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Match, Mount
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocketClose

from carrac import create_app
from carrac.fastapi_app import fastapi_app

# Paths flipped from Flask to FastAPI. Order doesn't matter (exact match).
# Add an entry here per migrated route and delete the Flask handler in the
# same change: a Flask route behind an entry here is unreachable, and
# tests/test_strangler_guard.py fails while one exists.
MIGRATED_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/health/ready",
        "/robots.txt",
        "/sitemap.xml",
        "/llms.txt",
        # Read-mostly authed endpoints (composite is THE proof point).
        "/api/batch",
        "/api/positions/live",
        "/api/plays/active",
        "/api/confluence",
        "/api/liq-heatmap",
        "/api/scanner",
        "/api/relative-strength",
        "/api/report/morning/latest",
        "/api/status",
        # Admin/billing JSON GETs.
        "/api/security-status",
        "/api/admin/costs",
        "/api/admin/assets",
        # Admin panel dashboards (billing overview / waitlist / usage).
        "/api/admin/billing/overview",
        "/api/admin/waitlist",
        "/api/admin/usage",
        # Admin MCP request-intent telemetry.
        "/api/admin/intents",
        "/api/admin/intents/unmet",
        "/api/billing/topup-check",
        "/api/billing/status",
        # Portfolio page read endpoints.
        "/api/portfolio/summary",
        "/api/portfolio/history",
    }
)

# Exact paths flipped only for specific HTTP methods. The Flask
# blueprint still serves the other methods. Tuple shape: (path, methods).
MIGRATED_WRITE_PATHS: tuple[tuple[str, frozenset[str]], ...] = (
    # POST /api/plays creates; GET /api/plays still on Flask.
    ("/api/plays", frozenset({"POST"})),
    # Journal writes. ExactPath for fixed paths (no params).
    # Note: /api/journal/trades/bulk MUST be an ExactPath so it is matched
    # BEFORE the /api/journal/trades/ PrefixPath entry that handles
    # PATCH/DELETE/POST (for /<id>/close). ExactPath entries evaluate first
    # in the dispatcher list, so there is no collision.
    ("/api/journal/trades", frozenset({"POST"})),
    ("/api/journal/trades/bulk", frozenset({"POST"})),
    ("/api/journal/sessions", frozenset({"POST"})),
    ("/api/journal/playbooks", frozenset({"POST"})),
    ("/api/journal/checklists", frozenset({"POST"})),
    ("/api/journal/goals", frozenset({"POST"})),
    # Watchlist writes.
    ("/api/watchlist", frozenset({"POST"})),
    ("/api/watchlist/add", frozenset({"POST"})),
    ("/api/watchlist/remove", frozenset({"POST"})),
    ("/api/watchlist/reorder", frozenset({"POST"})),
    ("/api/watchlist/groups", frozenset({"POST"})),
    # Widget token create (DELETE is in MIGRATED_WRITE_PREFIXES).
    ("/api/widget/tokens", frozenset({"POST"})),
    # Preferences writes.
    ("/api/indicator-prefs", frozenset({"POST"})),
    ("/api/preferences/sync", frozenset({"POST"})),
    ("/api/settings/save", frozenset({"POST"})),
    # Alerts writes. ExactPath for fixed sub-paths.
    # GET /api/alerts stays on Flask. PUT/DELETE /api/alerts/<id> are handled
    # by the method-filtered PrefixPath ("/api/alerts/", {PUT, DELETE}) below,
    # which only matches PUT and DELETE so these POST paths never collide.
    ("/api/alerts", frozenset({"POST"})),
    ("/api/alerts/trendline", frozenset({"POST"})),
    ("/api/alerts/indicator", frozenset({"POST"})),
    ("/api/alerts/formula", frozenset({"POST"})),
    ("/api/alerts/zscore", frozenset({"POST"})),
    ("/api/alerts/regime", frozenset({"POST"})),
    ("/api/alerts/chain_strength", frozenset({"POST"})),
    ("/api/alerts/anomaly_cluster", frozenset({"POST"})),
    ("/api/alerts/conditional", frozenset({"POST"})),
    ("/api/notification-settings", frozenset({"POST"})),
    ("/api/user/analyst-sources", frozenset({"POST"})),
    # Drawings writes. ExactPath for fixed paths.
    # GET /api/drawings stays on Flask. PUT/DELETE /api/drawings/<id> are
    # in MIGRATED_WRITE_PREFIXES below (method-filtered {PUT, DELETE}).
    ("/api/drawings", frozenset({"POST"})),
    ("/api/drawings/upsert", frozenset({"POST"})),
    ("/api/drawings/bulk", frozenset({"POST"})),
    ("/api/drawings/by-local-id", frozenset({"DELETE"})),
    # SSE + AI write paths. /api/ai/chat is the primary capacity
    # win (one asyncio task per stream vs. one gthread thread). /api/journal/*
    # AI subroutes are POST-only — the GETs stay on Flask.
    ("/api/ai/chat", frozenset({"POST"})),
    ("/api/ai/weekly-review", frozenset({"POST"})),
    ("/api/journal/ai-query", frozenset({"POST"})),
    ("/api/journal/ai-review/weekly", frozenset({"POST"})),
    # Remaining AI write surfaces. Non-streaming JSON; sync calls
    # wrapped in asyncio.to_thread so the event loop stays unblocked.
    ("/api/ai/execute-action", frozenset({"POST"})),
    ("/api/ai/context-preview", frozenset({"POST"})),
    # MCP JSON-RPC 2.0 endpoint. Sync dispatch wrapped in asyncio.to_thread
    # so tool handlers don't block the loop.
    ("/mcp", frozenset({"POST"})),
    # Paddle webhook. HMAC-only auth; CSRF skip already
    # in csrf_middleware._SKIP_PREFIXES.
    ("/api/billing/paddle-webhook", frozenset({"POST"})),
    # Admin/billing JSON POST endpoints.
    ("/api/csp-report", frozenset({"POST"})),
    ("/api/feedback", frozenset({"POST"})),
    ("/api/admin/assets", frozenset({"POST"})),
    ("/api/admin/waitlist/release", frozenset({"POST"})),
    ("/api/billing/portal", frozenset({"POST"})),
    # Chart shares. POST mints a frozen chart
    # share. An EXACT path on purpose — the broader ``/api/chart`` GET surface
    # (e.g. /api/chart-internal) is public + nginx-cached, so we do NOT widen a
    # prefix; only this exact write path flips to FastAPI. The slug-scoped
    # DELETE is a PrefixPath entry below.
    ("/api/chart/share", frozenset({"POST"})),
)

# Path-parameterised prefixes flipped only for specific methods.
# Trailing slash mandatory (same constraint as ``MIGRATED_PREFIXES``). The
# ``/api/plays/`` entry below sends every write under a play to FastAPI;
# the Flask blueprint keeps the GETs (``/api/plays/<id>``,
# ``/api/plays/<id>/backtest``).
MIGRATED_WRITE_PREFIXES: tuple[tuple[str, frozenset[str]], ...] = (
    # The methods set includes POST so the subroutes
    # (link-drawing/unlink-drawing/evaluate/backtest) reach the FastAPI
    # router. PUT covers /<id> update + /<id>/status; DELETE covers /<id>.
    # POST also catches /api/plays/<id> bare — FastAPI has no handler
    # there so it 404s, which Flask would 405 on (minor parity diff,
    # acceptable: clients never POST that path).
    ("/api/plays/", frozenset({"PUT", "DELETE", "POST"})),
    # Journal writes (path-parameterised).
    #
    # /api/journal/trades/ — catches PATCH + DELETE on /api/journal/trades/<id>
    # AND POST on /api/journal/trades/<id>/close (3-segment subroute). POST is
    # included so the /close subroute is forwarded; the /bulk exact-path entry
    # in MIGRATED_WRITE_PATHS is evaluated FIRST (ExactPath before PrefixPath
    # in the dispatcher), so /api/journal/trades/bulk POST never collides here.
    ("/api/journal/trades/", frozenset({"PATCH", "DELETE", "POST"})),
    # Extend sessions/ methods to include POST so
    # /api/journal/sessions/<id>/ai-summary forwards to FastAPI. The /<id>
    # bare PATCH/DELETE behavior is unchanged. The trades/ prefix already
    # includes POST (added for /<id>/close), so trade
    # ai-review at /api/journal/trades/<id>/ai-review is already covered.
    ("/api/journal/sessions/", frozenset({"PATCH", "DELETE", "POST"})),
    ("/api/journal/playbooks/", frozenset({"PATCH", "DELETE"})),
    ("/api/journal/checklists/", frozenset({"DELETE"})),
    # Widget token DELETE /api/widget/tokens/<id>.
    # Trailing slash matches descendant paths (/api/widget/tokens/<id>);
    # POST /api/widget/tokens (no trailing slash) is in MIGRATED_WRITE_PATHS
    # above so there is no conflict.
    ("/api/widget/tokens/", frozenset({"DELETE"})),
    # Alerts PUT/DELETE /api/alerts/<id>.
    # Method filter {PUT, DELETE} ensures POST /api/alerts/trendline etc.
    # (fixed ExactPaths above) never collide — ExactPaths evaluate first in
    # the dispatcher, and this prefix only fires on PUT or DELETE.
    ("/api/alerts/", frozenset({"PUT", "DELETE"})),
    # Drawings PUT/DELETE /api/drawings/<id>.
    # Same reasoning: ExactPaths for /upsert and /bulk POST are evaluated first.
    ("/api/drawings/", frozenset({"PUT", "DELETE"})),
    # DELETE /api/admin/assets/<pair> (pair is path-shaped:
    # 'SOL/1D' contains a slash, so the prefix shim is required).
    ("/api/admin/assets/", frozenset({"DELETE"})),
    # Chart shares — DELETE /api/chart/share/<slug>
    # revokes by slug (chart shares have no subject id). DELETE-only so the
    # POST /api/chart/share ExactPath above is unaffected; both are writes that
    # never collide with the public/cached /api/chart-internal GET surface.
    ("/api/chart/share/", frozenset({"DELETE"})),
)

# Path-parameterised routes the FastAPI sub-app owns. This list exists
# because the existing ``ExactPath`` shim cannot match templates
# like ``/api/asset/<symbol>/detail``. Each entry is a path PREFIX (with
# trailing slash) and the FastAPI router declares the suffix (e.g.
# ``@router.get("/api/asset/{symbol}/detail")``). The trailing slash matters:
# ``/api/asset/`` does NOT collide with the ``/api/assets`` (plural)
# blueprint, but a bare ``/api/asset`` would.
#
# Add a prefix here ONLY when a FastAPI router under that prefix has shipped.
# Otherwise the strangler forwards traffic to a 404 in production.
MIGRATED_PREFIXES: tuple[str, ...] = (
    "/api/asset/",  # /api/asset/<symbol>/detail
    # Chart-internal API-key endpoint; the payload is built by
    # carrac/charts/service.py::build_chart_payload.
    "/api/chart-internal/",
    # Phase 3 — per-asset facts bundle (cached, API-key auth, free tier).
    "/api/facts/",
)

# Chart front-door kill switch. Read ONCE at import (like every MIGRATED_* table
# above) — NOT re-read per request. Setting it to '0' is an
# instant rollback of GET /api/chart/<symbol> to 100% Flask, zero code
# change: MIGRATED_READ_PREFIXES below just becomes empty and the strangler
# falls straight through to the ``Mount("/", flask_asgi)`` catch-all exactly
# as it did before this front door existed.
CHART_ASGI_ENABLED: bool = os.getenv("CHART_ASGI_ENABLED", "1") == "1"

# Method-scoped path-parameterised prefix (same shape
# as MIGRATED_WRITE_PREFIXES, but for a READ): GET /api/chart/<symbol> flips
# to the FastAPI front door in fastapi_app/routers/chart_public.py, which
# resolves identity + applies the 120/minute rate limit off the asgiref
# single-thread bottleneck, then replays the UNCHANGED Flask view
# (carrac.charts.routes.get_chart_data) via asyncio.to_thread. GET-only on
# purpose:
#   * POST /api/chart/share is already an ExactPath entry in
#     MIGRATED_WRITE_PATHS and is evaluated BEFORE this prefix regardless.
#   * DELETE /api/chart/share/<slug> is already a PrefixPath entry in
#     MIGRATED_WRITE_PREFIXES for "/api/chart/share/" — disjoint by method
#     from this GET-only entry, so the two can never collide.
#   * No other GET exists under /api/chart/* in the Flask blueprint today
#     (verified: carrac/charts/routes.py registers exactly one route on
#     this path, ``/api/chart/<symbol>``).
MIGRATED_READ_PREFIXES: tuple[tuple[str, frozenset[str]], ...] = (
    (("/api/chart/", frozenset({"GET"})),) if CHART_ASGI_ENABLED else ()
)

# Screenshot front-door switch. Same convention as CHART_ASGI_ENABLED above:
# read once at import. Setting it to '0' routes the screenshot/chart-headless
# front doors to Flask instead of the ASGI app. Unlike CHART_ASGI_ENABLED=0,
# that is not a known-good fallback: Flask-direct screenshot rendering
# self-starves via asgiref's single shared thread (see
# fastapi_app/routers/screenshot_public.py's module docstring). The switch
# exists to isolate a fault in this front door, not as an emergency lever.
SCREENSHOT_ASGI_ENABLED: bool = os.getenv("SCREENSHOT_ASGI_ENABLED", "1") == "1"

# Screenshot + chart-headless front doors (see
# fastapi_app/routers/screenshot_public.py). Unlike MIGRATED_READ_PREFIXES,
# no method filter is needed: screenshot_chart / chart_headless /
# chart_headless_share are GET-only Flask routes with no sibling route on a
# different HTTP method under either path, so a plain (method-unfiltered)
# PrefixPath — the same shape as "/api/chart-internal/" and "/api/facts/"
# in MIGRATED_PREFIXES above — is sufficient. "/chart-headless/" covers BOTH
# chart_headless and chart_headless_share (screenshot_public.router
# declares the more specific "share/{slug}" route first so it wins the
# ambiguous two-segment match — see that module's docstring).
MIGRATED_SCREENSHOT_PREFIXES: tuple[str, ...] = (
    ("/api/screenshot/", "/chart-headless/") if SCREENSHOT_ASGI_ENABLED else ()
)


# sub-paths that a MIGRATED_WRITE_PREFIXES entry would otherwise
# swallow even though FastAPI has no handler for them. Each was verified dead
# in production (404/405) before being listed here, and each still has a live, working
# Flask handler; excluding them restores that handler. '*' matches exactly one
# path segment.
#
# This list is NOT the defence — tests/test_strangler_guard.py's reachability guard
# is, and it fails at merge the moment a ledger entry outruns what FastAPI
# actually implements. This list only covers entries that got past that guard.
# Deleting an entry here is correct ONLY once FastAPI genuinely serves
# that path; the guard will tell you.
EXCLUDED_SUBPATHS: dict[str, tuple[str, ...]] = {
    "/api/journal/trades/": (
        "/api/journal/trades/import",            # POST, Flask: journal/routes.py::import_from_drawings
        "/api/journal/trades/*/reopen",          # POST, Flask: journal/routes.py::reopen_trade
        "/api/journal/trades/*/screenshot",      # POST, Flask: journal/routes.py::capture_screenshot
    ),
    # "/api/drawings/" has no entry: the one route that needed excluding
    # (PUT /api/drawings/scenario/*/archive) no longer exists, so there is
    # nothing left to exclude.
}


class ExactPath(BaseRoute):
    """Starlette route that matches a single exact path and forwards
    the unmodified scope to the wrapped ASGI app.

    Unlike ``Mount``, this does NOT strip a prefix from ``scope['path']``
    — the wrapped app sees the original full path. That lets the
    FastAPI sub-app register handlers at their real path
    (``@app.get('/health')``) and still be reachable from the strangler
    dispatcher without a path rewrite.

    Optional ``methods`` filter. When supplied, only the listed
    HTTP methods route to the wrapped app — other methods fall through
    to the next router entry (typically the Flask mount). This is what
    lets ``POST /api/plays`` flip to FastAPI while ``GET /api/plays``
    stays on Flask.
    """

    def __init__(
        self, path: str, app, methods: frozenset[str] | None = None
    ) -> None:
        self.path = path
        self.app = app
        self.methods = (
            frozenset(m.upper() for m in methods) if methods else None
        )

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] != "http" or scope.get("path") != self.path:
            return Match.NONE, {}
        if self.methods is not None and scope.get("method", "GET").upper() not in self.methods:
            return Match.NONE, {}
        return Match.FULL, {}

    def url_path_for(self, name: str, /, **path_params):  # noqa: D401
        # Reverse-routing isn't supported for exact-path strangler entries;
        # callers should use Flask's url_for or the FastAPI app's own
        # router for FastAPI-native names.
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


class PrefixPath(BaseRoute):
    """Starlette route that matches a path prefix and forwards the
    unmodified scope to the wrapped ASGI app.

    The ExactPath shim can't match path-parameterised routes
    (``/api/sentiment/<asset>`` etc.). PrefixPath does NOT strip the
    prefix from ``scope['path']``, so the FastAPI router can declare
    handlers at their full real path with the path parameter
    (``@router.get("/api/sentiment/{asset}")``).

    The prefix MUST end with a trailing slash to avoid the
    ``/api/asset`` vs ``/api/assets`` collision (the latter is a
    different blueprint). A bare ``"/api/asset"`` prefix would
    incorrectly capture both.

    Optional ``methods`` filter — same role as on ExactPath.
    Lets ``PUT /api/plays/<id>`` flip to FastAPI while ``GET
    /api/plays/<id>`` (and its deeper subroutes still on Flask) stays.

    Optional ``exclude`` — exact paths, or regex-ish path shapes,
    under the prefix that must NOT be forwarded. A prefix entry is greedy: it
    swallows every sub-path beneath it, including ones FastAPI never
    implemented, and those requests then die at FastAPI's router instead of
    reaching the Flask handler that still serves them. Five routes were
    unreachable in production this way (see EXCLUDED_SUBPATHS below).

    Deliberately a static list, not a runtime "does FastAPI have this route?"
    check. That dynamic fall-through was considered and rejected: PrefixPath
    is shared with MIGRATED_SCREENSHOT_PREFIXES, where falling back to Flask
    is the screenshot-starvation failure itself (see SCREENSHOT_ASGI_ENABLED above), so
    a silent auto-revert there would be worse than a loud 404. The class is
    prevented at merge time instead, by the reachability guard in
    tests/test_strangler_guard.py.
    """

    def __init__(
        self, prefix: str, app, methods: frozenset[str] | None = None,
        exclude: tuple[str, ...] = (),
    ) -> None:
        if not prefix.endswith("/"):
            raise ValueError(
                f"PrefixPath prefix must end with '/' to avoid collisions: {prefix!r}"
            )
        self.prefix = prefix
        self.app = app
        self.methods = (
            frozenset(m.upper() for m in methods) if methods else None
        )
        self.exclude = exclude

    def _excluded(self, path: str) -> bool:
        """True when *path* is an un-migrated sub-path that must stay on Flask.

        Segments are compared with '*' standing for exactly one path segment,
        so '/api/journal/trades/*/reopen' matches trade id 7 but not a deeper
        path. Matching one segment (never a slash) is what keeps an exclusion
        from silently widening as new sub-routes are added underneath it.
        """
        got = path.rstrip("/").split("/")
        for pattern in self.exclude:
            want = pattern.rstrip("/").split("/")
            if len(want) != len(got):
                continue
            if all(w == "*" or w == g for w, g in zip(want, got)):
                return True
        return False

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.prefix):
            return Match.NONE, {}
        if self.methods is not None and scope.get("method", "GET").upper() not in self.methods:
            return Match.NONE, {}
        if self.exclude and self._excluded(scope.get("path", "")):
            return Match.NONE, {}
        return Match.FULL, {}

    def url_path_for(self, name: str, /, **path_params):  # noqa: D401
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Curl-endpoint dispatcher
# ---------------------------------------------------------------------------

# Single-segment path that looks like a tracked asset or a curl helper.
# Matches: /<symbol> and /<symbol>@<window>  (no deeper paths).
# E.g. /btc, /aapl@1w, /:help, /:assets.
_CURL_PATH_RE = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9^.\-]{0,11})(@[A-Za-z0-9]{1,10})?$")
# Browser UA check — used for symbol paths (router 302s browsers to landing).
_BROWSER_UA_RE = re.compile(rb"Mozilla|Chrome|Safari|Edg", re.IGNORECASE)
# Terminal-tool UA check — used ONLY for the bare-/ dispatch so programmatic
# non-browser clients (Python scripts, monitoring, test clients using
# python-httpx/requests/aiohttp) still reach the Flask landing page as
# before.  Curl, wget, HTTPie and direct HTTP tools are the intended
# terminal audience; everything else that isn't a browser or a terminal tool
# falls through to Flask unchanged.
_CURL_TOOL_UA_RE = re.compile(rb"curl/|wget/|HTTPie/|httpie/|HTTPClient/|Go-http|Rust|python-pycurl", re.IGNORECASE)
# The curl helper paths served by the FastAPI router regardless of UA.
_CURL_EXACT = frozenset({"/:help", "/:assets", "/:mcp"})
# Dual-mode funnel paths: routed to FastAPI for ANY UA, but the router branches
# on UA (terminal → ANSI register page; browser → 302 to /register). They are
# NEW short paths (Flask owns /register, not /reg or /reg-tui) so they never
# collide with the Flask url_map; we intercept them here BEFORE the
# symbol/registry check so an untracked single segment can't shadow them.
# /reg-tui is the TUI-attributed twin printed by the carrac binary's gates.
_CURL_REG = frozenset({"/reg", "/reg-tui"})


class CurlChartPath(BaseRoute):
    """Route curl-endpoint traffic to FastAPI.

    Matches, in order:
      1. ``/:help`` and ``/:assets`` — any UA, forwarded as-is.
      2. Bare ``GET /`` for non-browser UAs — rewritten to ``/curl-overview``
         (the dispatcher rewrites; the router doesn't know it came from /).
      3. ``GET /<symbol>[@window]`` where <symbol> is a tracked asset in the
         registry whitelist — any UA (the FastAPI router 302s browsers to the
         landing page internally, so the dispatcher is UA-agnostic for these).

    Single-segment Flask paths (login, pricing, mcp, …) are excluded via
    the Flask url_map check (``_reserved_paths``), so nothing existing
    changes hands.  The registry resolution fails-closed: if the symbol DB
    is unreachable, ``registry.resolve`` returns None and we fall through to
    Flask (safe 404 rather than an erroneous chart attempt).

    Everything that doesn't match falls through to the next handler in the
    Starlette routes list (the Flask ``Mount("/")``) because Starlette
    iterates routes in order and calls ``handle()`` only on the first
    ``Match.FULL`` hit.  When we return ``Match.NONE`` the router moves on.
    """

    def __init__(self, app) -> None:
        self.app = app  # FastAPI ASGI callable
        self._reserved: frozenset[str] | None = None
        self._reserved_lower: frozenset[str] | None = None

    def _reserved_paths(self) -> frozenset[str]:
        """Lazy-load the set of single-segment Flask paths (lower-cased)."""
        if self._reserved_lower is None:
            rules: set[str] = set()
            for rule in flask_app.url_map.iter_rules():
                p = rule.rule
                # Single-segment: exactly one slash, no path parameters,
                # not the root itself.
                if p.count("/") == 1 and "<" not in p and p != "/":
                    rules.add(p.lower())
            self._reserved_lower = frozenset(rules)
        return self._reserved_lower

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        # Accept HEAD as well as GET: link-preview bots and uptime/link checkers
        # probe the funnel paths (/reg, /reg-tui, /btc, …) with HEAD. The FastAPI
        # router auto-handles HEAD on its GET routes (Starlette runs the handler
        # and strips the body), so forwarding HEAD here yields a 200 with the
        # right headers instead of a 404 from the Flask fall-through.
        if scope["type"] != "http" or scope.get("method", "GET").upper() not in (
            "GET",
            "HEAD",
        ):
            return Match.NONE, {}
        path: str = scope.get("path", "")

        # 1. Curl helper paths — match any UA, no registry lookup needed.
        #    /reg is dual-mode (router branches on UA) but still matches here
        #    for all UAs; it must win before the symbol/registry check below.
        if path in _CURL_EXACT or path in _CURL_REG:
            return Match.FULL, {}

        # 2. Bare GET / — match only for explicit terminal-tool UAs (curl,
        #    wget, HTTPie, etc.).  Browsers always go to Flask landing.
        #    Programmatic non-browser clients (python-httpx, requests, …)
        #    that aren't curl-like also fall through to Flask so existing
        #    tests and integrations see the HTML landing unchanged.
        if path == "/":
            headers = dict(scope.get("headers") or [])
            ua = headers.get(b"user-agent", b"")
            if _CURL_TOOL_UA_RE.search(ua):
                return Match.FULL, {}
            return Match.NONE, {}  # browser or non-curl script → Flask landing

        # 3. Single-segment symbol (with optional @window suffix).
        m = _CURL_PATH_RE.match(path)
        if not m:
            return Match.NONE, {}

        # Skip paths that Flask owns (login, pricing, mcp, …).
        if path.lower() in self._reserved_paths():
            return Match.NONE, {}

        # Registry check — fail-closed (None → not tracked → fall through).
        from carrac.curl_charts import registry  # late import: no DB at import time
        if registry.resolve(m.group(1)) is None:
            return Match.NONE, {}

        return Match.FULL, {}

    def url_path_for(self, name: str, /, **path_params):  # noqa: D401
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        if path == "/":
            # Rewrite bare / to the internal curl-overview path so the
            # FastAPI router can match the @router.get("/curl-overview") handler.
            scope = dict(scope)
            scope["path"] = "/curl-overview"
            scope["raw_path"] = b"/curl-overview"
        await self.app(scope, receive, send)


# Late import: avoid circular surprise if starlette restructures.
from starlette.routing import NoMatchFound  # noqa: E402

# ---------------------------------------------------------------------------
# Pin the Flask bridge to its own executor.
#
# asgiref decorates ``WsgiToAsgiInstance.run_wsgi_app`` with a bare
# ``@sync_to_async``, i.e. ``thread_sensitive=True``.  That makes it resolve
# its executor from shared state instead of a fixed pool::
#
#     current_thread_executor = getattr(AsyncToSync.executors, "current", None)
#     if current_thread_executor:
#         executor = current_thread_executor      # <-- borrowed
#
# and EVERY in-flight WSGI request publishes onto that Local, because
# ``WsgiToAsgiInstance.__call__`` builds ``self.sync_send = AsyncToSync(send)``.
# So under concurrency one request submits its WSGI work onto a sibling
# request's ``CurrentThreadExecutor``; when the sibling finishes, that executor
# quits and the borrower dies with "CurrentThreadExecutor already quit or is
# broken" — a 500 on whatever Flask route was next.  In production that was
# ``GET /login``, since every login-gated page redirects there and the redirect
# is followed while the first request is still in flight.
#
# Reproduced on a full stack (concurrent redirect-follows: ~50% of non-rate-limited
# requests 500'd, warm and cold alike) and deterministically in
# ``tests/test_asgi_wsgi_executor.py``.
#
# max_workers stays at 1: that is exactly what asgiref's
# ``SyncToAsync.single_thread_executor`` gave us before, and the two front
# doors built around that limit (chart, screenshot)
# assume it.  Raising it is a real behaviour change.
FLASK_WSGI_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="flask-wsgi"
)


class _IsolatedWsgiToAsgiInstance(WsgiToAsgiInstance):
    """``WsgiToAsgiInstance`` whose WSGI call cannot borrow a sibling's executor."""

    # Unwrap asgiref's SyncToAsync descriptor to get at the plain function —
    # attribute access on the class returns a bound functools.partial instead.
    run_wsgi_app = sync_to_async(
        WsgiToAsgiInstance.__dict__["run_wsgi_app"].func,
        thread_sensitive=False,
        executor=FLASK_WSGI_EXECUTOR,
    )


class IsolatedWsgiToAsgi(WsgiToAsgi):
    """``WsgiToAsgi`` that hands off to :class:`_IsolatedWsgiToAsgiInstance`.

    Internet scanners open raw WebSockets to ``ws://<ip>/``.
    Carrac declares no WebSocket route anywhere (checked ``carrac/`` and
    ``carrac/fastapi_app/`` for ``WebSocketRoute`` / ``@app.websocket`` /
    a Starlette websocket route — none exist; the only "websocket" hits are
    outbound WS *clients* to Binance/Hyperliquid, not endpoints we serve), so
    Starlette's router never matches these scopes and they fall through this
    ``Mount("/")`` to the Flask bridge. ``asgiref``'s
    ``WsgiToAsgiInstance.__call__`` raises ``ValueError("WSGI wrapper
    received a non-HTTP scope")`` on anything but ``scope["type"] == "http"``,
    which Starlette surfaces as an unhandled exception and Bugsink records as
    one. Close the handshake instead — same helper and default code (1000,
    Normal Closure) Starlette itself uses when a websocket scope matches no
    route — rather than raising through a client that was never accepted.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await WebSocketClose()(scope, receive, send)
            # Any other non-HTTP scope type (lifespan is routed to the
            # Mount("/api/v2", ...) branch, never here — see module
            # docstring) is quietly ignored rather than raised through.
            return
        await _IsolatedWsgiToAsgiInstance(
            self.wsgi_application, self.duplicate_header_limit
        )(scope, receive, send)


flask_app = create_app()
flask_asgi = IsolatedWsgiToAsgi(flask_app)


asgi_app = Starlette(
    routes=[
        Mount("/api/v2", app=fastapi_app),
        # Order matters: exact paths first (most specific), then method-
        # filtered exact paths, then prefixes, then prefix+method, then
        # Flask fallthrough. The exact paths and the
        # method-scoped entries do not collide because the
        # method-scoped paths are NEW resources for the FastAPI side
        # (Flask blueprint stays serving the unfiltered methods).
        *(ExactPath(p, app=fastapi_app) for p in sorted(MIGRATED_PATHS)),
        *(
            ExactPath(p, app=fastapi_app, methods=m)
            for p, m in MIGRATED_WRITE_PATHS
        ),
        *(PrefixPath(p, app=fastapi_app) for p in MIGRATED_PREFIXES),
        *(
            PrefixPath(p, app=fastapi_app, methods=m,
                       exclude=EXCLUDED_SUBPATHS.get(p, ()))
            for p, m in MIGRATED_WRITE_PREFIXES
        ),
        # GET /api/chart/<symbol> front door. Empty tuple (see
        # CHART_ASGI_ENABLED above) when the kill switch is off, in which
        # case this contributes zero routes and the request falls straight
        # through to the Flask Mount("/") below, unchanged.
        *(
            PrefixPath(p, app=fastapi_app, methods=m)
            for p, m in MIGRATED_READ_PREFIXES
        ),
        # Screenshot + chart-headless front doors. Empty tuple
        # (see SCREENSHOT_ASGI_ENABLED above) when the kill switch is off.
        *(PrefixPath(p, app=fastapi_app) for p in MIGRATED_SCREENSHOT_PREFIXES),
        # Curl-endpoint dispatcher: routes curl-shaped GETs to the FastAPI
        # chart router. Must sit BEFORE the Flask Mount("/") catch-all so
        # non-browser GETs to tracked symbols reach the chart handler.
        # Browsers hitting bare / still fall through to Flask (Match.NONE).
        CurlChartPath(app=fastapi_app),
        Mount("/", app=flask_asgi),
    ]
)
