"""Strangler-fig CI guard: the routing tables in carrac/asgi.py and the Flask
url_map must agree.

Two failure modes, both enforced:

* **Unreachable Flask code.** A Flask handler on a method + path that the
  dispatcher sends to FastAPI first never runs. It still compiles, still
  passes its Flask test-client tests (``app.test_client()`` skips the
  dispatcher), and quietly diverges from the FastAPI handler that really
  serves the path. ``test_no_flask_handler_is_shadowed_by_fastapi`` drives the
  real composed router and is the authoritative check; the three table sweeps
  ask the same question per routing table, so a failure names the table.
  The only Flask views allowed behind a FastAPI route are the front doors in
  ``FRONT_DOOR_VIEWS``: FastAPI handles identity and rate limits, then calls
  the Flask view itself, and a kill switch can route traffic back to it.
* **Routes forwarded to nothing.** A prefix entry is greedy; if the dispatcher
  forwards a path FastAPI does not implement, the request dies at FastAPI's
  router. ``test_no_migrated_route_forwards_to_a_missing_fastapi_handler``
  catches that, and ``EXCLUDED_SUBPATHS`` hands such paths back to Flask.
* **Unreachable FastAPI code.** The reverse of the first: a FastAPI handler
  whose method + path the dispatcher never forwards is served by Flask (or by
  nothing) at its own path, and is reachable only under the ``/api/v2`` mount.
  ``test_every_fastapi_route_is_reachable_at_its_own_path`` drives every
  FastAPI route through the real composed router.
"""

from __future__ import annotations

import re
from typing import Iterator

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

from carrac.asgi import (
    EXCLUDED_SUBPATHS,
    MIGRATED_PATHS,
    MIGRATED_PREFIXES,
    MIGRATED_READ_PREFIXES,
    MIGRATED_SCREENSHOT_PREFIXES,
    MIGRATED_WRITE_PATHS,
    MIGRATED_WRITE_PREFIXES,
)
from carrac.fastapi_app import fastapi_app

# Flask views that FastAPI front doors call directly (and that their kill
# switches, CHART_ASGI_ENABLED and SCREENSHOT_ASGI_ENABLED, route back to).
FRONT_DOOR_VIEWS = frozenset({
    "charts.get_chart_data",
    "charts.chart_headless",
    "charts.chart_headless_share",
    "screenshots.screenshot_chart",
})


def _iter_flask_rules(app) -> Iterator[tuple[str, frozenset[str]]]:
    """Yield (rule_string, methods) for every Flask url_map rule.

    Excludes auto-generated HEAD and OPTIONS.
    """
    for rule in app.url_map.iter_rules():
        methods = frozenset(rule.methods or []) - {"HEAD", "OPTIONS"}
        if methods:
            yield rule.rule, methods


_FLASK_PARAM = re.compile(r"<(?:[^:>]+:)?([^>]+)>")


def _probe_path(rule: str) -> str:
    """A concrete path for a Flask rule, e.g. /api/journal/trades/<int:id>/close
    -> /api/journal/trades/1/close, so the real router can be asked about it."""
    return _FLASK_PARAM.sub("1", rule)


def _fastapi_serves(path: str, method: str) -> bool:
    scope = {"type": "http", "path": path, "method": method,
             "headers": [], "query_string": b"", "root_path": ""}
    return any(r.matches(scope)[0] == Match.FULL for r in fastapi_app.routes)


def _forwarded_to_fastapi(path: str, method: str) -> bool:
    """Mirror the dispatch decision in carrac/asgi.py's route list."""
    if path in MIGRATED_PATHS:
        return True
    for p, methods in MIGRATED_WRITE_PATHS:
        if path == p and method in methods:
            return True
    for p in MIGRATED_PREFIXES:
        if path.startswith(p):
            return True
    for p, methods in MIGRATED_WRITE_PREFIXES:
        if path.startswith(p) and method in methods:
            if _excluded_by_ledger(p, path):
                continue
            return True
    for p, methods in MIGRATED_READ_PREFIXES:
        if path.startswith(p) and method in methods:
            return True
    for p in MIGRATED_SCREENSHOT_PREFIXES:
        if path.startswith(p):
            return True
    return False


def _excluded_by_ledger(prefix: str, path: str) -> bool:
    got = path.rstrip("/").split("/")
    for pattern in EXCLUDED_SUBPATHS.get(prefix, ()):
        want = pattern.rstrip("/").split("/")
        if len(want) == len(got) and all(w == "*" or w == g for w, g in zip(want, got)):
            return True
    return False


def _flask_view_rules(app) -> Iterator[tuple[str, str, frozenset[str]]]:
    """Yield (rule, endpoint, methods) for every Flask rule except the front doors."""
    for rule in app.url_map.iter_rules():
        if rule.endpoint in FRONT_DOOR_VIEWS:
            continue
        methods = frozenset(rule.methods or []) - {"HEAD", "OPTIONS"}
        if methods:
            yield rule.rule, rule.endpoint, methods


# ---------------------------------------------------------------------------
# Unreachable Flask code
# ---------------------------------------------------------------------------

def test_no_flask_handler_is_shadowed_by_fastapi():
    """Run every Flask rule x method through the composed dispatcher; none may
    be sent to FastAPI first."""
    from carrac.asgi import asgi_app, flask_app, flask_asgi

    shadowed = []
    for rule, endpoint, methods in _flask_view_rules(flask_app):
        for method in sorted(methods):
            scope = {"type": "http", "path": _probe_path(rule), "method": method,
                     "headers": [], "query_string": b"", "root_path": ""}
            first = next(r for r in asgi_app.routes if r.matches(scope)[0] == Match.FULL)
            if getattr(first, "app", None) is not flask_asgi:
                shadowed.append(f"  {method:7} {rule}  ({endpoint})")
    assert not shadowed, (
        "Flask handlers the dispatcher never reaches (FastAPI serves these "
        "first) — delete them:\n" + "\n".join(shadowed)
    )


def test_no_flask_handler_for_migrated_exact_paths(app):
    """No Flask blueprint may register any handler on a MIGRATED_PATHS entry."""
    violations = [
        f"  Flask rule '{rule}' methods={sorted(methods)} shadows MIGRATED_PATHS entry"
        for rule, _endpoint, methods in _flask_view_rules(app)
        if rule in MIGRATED_PATHS
    ]
    assert not violations, (
        "Flask handlers found on MIGRATED_PATHS routes:\n" + "\n".join(violations)
    )


def test_no_flask_handler_for_migrated_write_paths(app):
    """No Flask blueprint may register a method that MIGRATED_WRITE_PATHS
    sends to FastAPI on the same exact path."""
    migrated = dict(MIGRATED_WRITE_PATHS)
    violations = []
    for rule, _endpoint, methods in _flask_view_rules(app):
        shadow = methods & migrated.get(rule, frozenset())
        if shadow:
            violations.append(
                f"  Flask rule '{rule}' registers {sorted(shadow)} which "
                f"MIGRATED_WRITE_PATHS sends to FastAPI"
            )
    assert not violations, (
        "Flask handlers shadow MIGRATED_WRITE_PATHS entries:\n" + "\n".join(violations)
    )


def test_no_flask_handler_for_migrated_write_prefixes(app):
    """No Flask blueprint may register a method + path that a
    MIGRATED_WRITE_PREFIXES entry forwards, unless EXCLUDED_SUBPATHS hands
    that path back to Flask."""
    violations = []
    for rule, _endpoint, methods in _flask_view_rules(app):
        probe = _probe_path(rule)
        for prefix, migrated_methods in MIGRATED_WRITE_PREFIXES:
            if not probe.startswith(prefix) or _excluded_by_ledger(prefix, probe):
                continue
            shadow = methods & migrated_methods
            if shadow:
                violations.append(
                    f"  Flask rule '{rule}' registers {sorted(shadow)} under "
                    f"'{prefix}', which MIGRATED_WRITE_PREFIXES sends to FastAPI"
                )
    assert not violations, (
        "Flask handlers shadow MIGRATED_WRITE_PREFIXES entries:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Unreachable FastAPI code
# ---------------------------------------------------------------------------

_FASTAPI_PARAM = re.compile(r"\{[^}]+\}")

# FastAPI routes the dispatcher deliberately does not forward at the probed
# path, each with the reason.
FASTAPI_ROUTES_NOT_AT_OWN_PATH = {
    ("GET", "/_ping"): "internal probe, served only under /api/v2 (carrac/asgi.py docstring)",
    ("GET", "/_whoami"): "env-gated debug route, served only under /api/v2",
    ("GET", "/curl-overview"): "CurlChartPath rewrites a non-browser GET / to it",
    ("GET", "/{symbol_spec}"): "CurlChartPath forwards it for tracked symbols only; the probe is not one",
}


def _fastapi_leaf_routes(routes) -> Iterator[APIRoute]:
    """Every APIRoute, including those inside included routers."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _fastapi_leaf_routes(included.routes)
        elif isinstance(route, APIRoute):
            yield route


def test_every_fastapi_route_is_reachable_at_its_own_path():
    """Run every FastAPI route x method through the composed dispatcher; each
    must be sent to FastAPI at its own path, not only under /api/v2."""
    from carrac.asgi import asgi_app

    unreachable = []
    for route in _fastapi_leaf_routes(fastapi_app.routes):
        probe = _FASTAPI_PARAM.sub("1", route.path)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            assert _fastapi_serves(probe, method), (
                f"{method} {probe} (from {route.path}) does not match FastAPI "
                "itself, so this guard rebuilds route paths wrongly"
            )
            scope = {"type": "http", "path": probe, "method": method,
                     "headers": [], "query_string": b"", "root_path": ""}
            first = next(r for r in asgi_app.routes if r.matches(scope)[0] == Match.FULL)
            if (getattr(first, "app", None) is not fastapi_app
                    and (method, route.path) not in FASTAPI_ROUTES_NOT_AT_OWN_PATH):
                unreachable.append(f"  {method:7} {route.path}  ({route.name})")
    assert not unreachable, (
        "FastAPI handlers the dispatcher never forwards at their own path "
        "(Flask, or nothing, serves these). Route them in carrac/asgi.py or "
        "delete them:\n" + "\n".join(unreachable)
    )


# ---------------------------------------------------------------------------
# Routes forwarded to nothing
# ---------------------------------------------------------------------------
# Uses Starlette's own matcher rather than comparing path templates as
# strings: ``fastapi_app.routes`` holds included-router proxies whose leaf
# routes are not exposed as ``.path``, so a string comparison would report
# every router-included route as missing.

def test_no_migrated_route_forwards_to_a_missing_fastapi_handler(app):
    """Every Flask route the strangler forwards to FastAPI must be servable there.

    Fails loudly at merge, before any request is misrouted.
    """
    dead: list[str] = []
    for rule, methods in _iter_flask_rules(app):
        probe = _probe_path(rule)
        for method in sorted(methods):
            if not _forwarded_to_fastapi(probe, method):
                continue
            if not _fastapi_serves(probe, method):
                dead.append(f"  {method:7} {rule}  -> forwarded to FastAPI, which has no route")
    assert not dead, (
        "Routes forwarded to FastAPI that FastAPI cannot serve — these are DEAD "
        "in production (the Flask handler is never reached):\n" + "\n".join(dead)
        + "\n\nEither implement the FastAPI handler, or add the path to "
          "EXCLUDED_SUBPATHS in carrac/asgi.py so it stays on Flask."
    )


def _matches_pattern(pattern: str, path: str) -> bool:
    want, got = pattern.rstrip("/").split("/"), path.rstrip("/").split("/")
    return len(want) == len(got) and all(w == "*" or w == g for w, g in zip(want, got))


def test_every_excluded_subpath_is_still_needed(app):
    """An EXCLUDED_SUBPATHS entry whose FastAPI handler now exists is stale —
    it would pin traffic to Flask forever. Keeps the exclusion list honest so
    it cannot quietly become a permanent migration blocker.

    Method-aware on purpose. FastAPI's ``/api/journal/trades/{trade_id}``
    matches ``/api/journal/trades/import`` for PATCH and DELETE by capturing
    "import" as the id — that near-miss is the very reason the route returned
    405 rather than 404. Only the methods Flask actually serves on the path
    tell you whether the exclusion is still doing real work.
    """
    stale = []
    for prefix, patterns in EXCLUDED_SUBPATHS.items():
        for pattern in patterns:
            probe = pattern.replace("*", "1")
            flask_methods: set[str] = set()
            for rule, methods in _iter_flask_rules(app):
                if _matches_pattern(pattern, _probe_path(rule)):
                    flask_methods |= set(methods)
            if not flask_methods:
                stale.append(f"  {pattern} is excluded but no Flask route serves it either")
                continue
            served = [m for m in sorted(flask_methods) if _fastapi_serves(probe, m)]
            if served:
                stale.append(f"  {pattern} is excluded but FastAPI now serves it for {served}")
    assert not stale, (
        "Stale EXCLUDED_SUBPATHS entries in carrac/asgi.py — either FastAPI can "
        "serve these now (drop the exclusion) or nothing serves them at all "
        "(drop the route):\n" + "\n".join(stale)
    )


# --- Dispatch behaviour, not just table consistency --------------------------
# The guard above compares tables. This drives the real composed ASGI app, so a
# dispatcher bug that both tables agree about still gets caught.

@pytest.mark.parametrize("method,path", [
    ("POST", "/api/journal/trades/import"),
    ("POST", "/api/journal/trades/1/reopen"),
    ("POST", "/api/journal/trades/1/screenshot"),
    # A removed route ("PUT", "/api/drawings/scenario/1/archive") — the route
    # it guarded is deleted, so this case would now assert that a nonexistent
    # endpoint reaches Flask, which is not a thing worth being true.
])
def test_excluded_subpaths_reach_flask_not_a_fastapi_dead_end(method, path):
    """These must reach their Flask handler and be auth-gated.

    A 404/405 means the prefix swallowed them again and the Flask handler is
    unreachable — the exact production bug. 302 (Flask login redirect) or 401
    both mean the handler was reached and rejected the anonymous caller, which
    is the correct behaviour for an unauthenticated request.
    """
    from starlette.testclient import TestClient

    from carrac.asgi import asgi_app

    resp = TestClient(asgi_app).request(method, path, follow_redirects=False)
    assert resp.status_code in (301, 302, 401, 403), (
        f"{method} {path} returned {resp.status_code} — expected an auth "
        f"response from the Flask handler. 404/405 means the strangler "
        f"swallowed it again."
    )


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/journal/trades/1/close"),   # PrefixPath -> FastAPI
    ("PUT", "/api/drawings/1"),                # PrefixPath -> FastAPI
    ("POST", "/api/journal/trades/bulk"),      # ExactPath, evaluated first
])
def test_migrated_siblings_still_route_to_fastapi(method, path):
    """The exclusions must not widen. These share the same prefixes and are
    genuinely migrated, so they must keep reaching FastAPI."""
    from starlette.testclient import TestClient

    from carrac.asgi import asgi_app

    resp = TestClient(asgi_app).request(method, path, follow_redirects=False)
    assert resp.status_code in (301, 302, 401, 403), (
        f"{method} {path} returned {resp.status_code} — a migrated route "
        f"stopped resolving; an exclusion pattern may have widened."
    )
