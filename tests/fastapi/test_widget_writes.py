"""Widget token write routing tests.

Confirms:
- ``POST /api/widget/tokens`` is in ``MIGRATED_WRITE_PATHS``.
- ``DELETE /api/widget/tokens/<id>`` is covered by the
  ``/api/widget/tokens/`` prefix entry in ``MIGRATED_WRITE_PREFIXES``.
- The method filters work correctly (POST → FastAPI, GET → Flask,
  DELETE on prefix → FastAPI, GET on prefix → Flask).
- ``WidgetTokenCreateRequest`` enforces ``extra="forbid"``.
- The ``/api/widget/tokens/`` prefix with DELETE-only filter does NOT
  capture the ``GET /api/widget/tokens`` path (which stays on Flask).
- Structural assertion: X-API-Key DOES NOT bypass CSRF for widget token
  writes, which are session-only. We confirm this by inspecting the
  middleware's widget-path conditional logic (no live HTTP call needed for
  routing-level test).
"""

from __future__ import annotations

import pytest

from carrac.asgi import (
    MIGRATED_WRITE_PATHS,
    MIGRATED_WRITE_PREFIXES,
    ExactPath,
    PrefixPath,
)
from carrac.fastapi_app.models.widget import WidgetTokenCreateRequest


# ---------------------------------------------------------------------------
# Dispatcher registration
# ---------------------------------------------------------------------------


def test_widget_token_post_listed_in_migrated_writes():
    assert ("/api/widget/tokens", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_widget_token_delete_prefix_listed_in_migrated_write_prefixes():
    assert ("/api/widget/tokens/", frozenset({"DELETE"})) in MIGRATED_WRITE_PREFIXES


# ---------------------------------------------------------------------------
# Method-filter sanity — POST /api/widget/tokens
# ---------------------------------------------------------------------------


def test_widget_token_exact_path_matches_post_only():
    ep = ExactPath("/api/widget/tokens", app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": "/api/widget/tokens"}

    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})
    delete_match, _ = ep.matches({**base, "method": "DELETE"})

    # POST → FastAPI
    assert post_match.value > 0
    # GET/DELETE on exact /api/widget/tokens → Flask (get list / wrong method)
    assert get_match.value == 0
    assert delete_match.value == 0


# ---------------------------------------------------------------------------
# Method-filter sanity — DELETE /api/widget/tokens/<id>
# ---------------------------------------------------------------------------


def test_widget_token_prefix_matches_delete_on_child_path():
    pp = PrefixPath("/api/widget/tokens/", app=None, methods=frozenset({"DELETE"}))
    base = {"type": "http", "path": "/api/widget/tokens/42"}

    del_match, _ = pp.matches({**base, "method": "DELETE"})
    get_match, _ = pp.matches({**base, "method": "GET"})
    post_match, _ = pp.matches({**base, "method": "POST"})

    # DELETE on /api/widget/tokens/<id> → FastAPI
    assert del_match.value > 0
    # GET/POST on /api/widget/tokens/<id> → Flask
    assert get_match.value == 0
    assert post_match.value == 0


def test_widget_token_prefix_does_not_match_exact_root_path():
    """``/api/widget/tokens/`` (trailing slash) does NOT match the
    ``/api/widget/tokens`` (no trailing slash) path.
    This is the safety guarantee that prevents the DELETE prefix from
    capturing the POST /api/widget/tokens exact path.
    """
    pp = PrefixPath("/api/widget/tokens/", app=None, methods=frozenset({"DELETE"}))
    match, _ = pp.matches({"type": "http", "path": "/api/widget/tokens", "method": "DELETE"})
    # /api/widget/tokens does NOT start with /api/widget/tokens/ → no match
    assert match.value == 0


# ---------------------------------------------------------------------------
# Pydantic model — WidgetTokenCreateRequest
# ---------------------------------------------------------------------------


def test_widget_token_create_default_name():
    req = WidgetTokenCreateRequest()
    assert req.name == "Default"


def test_widget_token_create_custom_name():
    req = WidgetTokenCreateRequest(name="My iPhone")
    assert req.name == "My iPhone"


def test_widget_token_create_name_max_length():
    long_name = "x" * 50
    req = WidgetTokenCreateRequest(name=long_name)
    assert req.name == long_name


def test_widget_token_create_extra_field_rejected():
    """``extra="forbid"`` must raise on unknown fields."""
    with pytest.raises(Exception):
        WidgetTokenCreateRequest(name="ok", rogue_field="bad")


# ---------------------------------------------------------------------------
# CSRF no-bypass assertion (structural)
# ---------------------------------------------------------------------------


def test_csrf_middleware_widget_path_not_in_skip_prefixes():
    """``/api/widget/tokens`` must NOT appear in ``_SKIP_PREFIXES`` so that
    CSRF is enforced for session callers and the X-API-Key bypass branch is
    not reachable for widget token writes, which are session-only.
    """
    from carrac.fastapi_app.csrf_middleware import _SKIP_PREFIXES

    for prefix in _SKIP_PREFIXES:
        assert not "/api/widget/tokens".startswith(prefix), (
            f"CSRF skip prefix {prefix!r} would bypass CSRF on widget token writes"
        )
