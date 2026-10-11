"""API keys are accepted in a request header only, never as a ``?key=`` query
parameter: a query string ends up in access logs, so a key passed there leaks.

Each case sends the same valid key twice through the composed ASGI app — once
as ``?key=`` with no header (must be refused) and once as ``X-API-Key`` (must
be served) — so the test proves both that the query form is rejected and that
the header form still works on the same route.
"""

from __future__ import annotations

import os

import pytest

PER_USER_KEY_ROUTES = [
    "/chart-headless/BTC/1D",
    "/api/drawings?asset=BTC",
    "/api/history/context",
    "/api/indicator-prefs",
]


@pytest.mark.parametrize("path", PER_USER_KEY_ROUTES)
def test_query_param_key_is_refused(asgi_client, member_key, path):
    headers, _uid = member_key
    raw_key = headers["X-API-Key"]
    sep = "&" if "?" in path else "?"

    resp = asgi_client.get(f"{path}{sep}key={raw_key}")
    assert resp.status_code == 401, (
        f"GET {path} with ?key= and no header returned {resp.status_code}; "
        f"a query-string key must not authenticate"
    )


@pytest.mark.parametrize("path", PER_USER_KEY_ROUTES)
def test_header_key_is_accepted(asgi_client, member_key, path):
    headers, _uid = member_key
    resp = asgi_client.get(path, headers=headers)
    assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])


def test_chart_headless_refuses_query_param_key(asgi_client):
    key = os.environ["SCREENSHOT_API_KEY"]
    resp = asgi_client.get(f"/chart-headless/BTC/1D?key={key}")
    assert resp.status_code == 401


def test_chart_headless_accepts_internal_key_header(asgi_client):
    key = os.environ["SCREENSHOT_API_KEY"]
    resp = asgi_client.get("/chart-headless/BTC/1D", headers={"X-Internal-Key": key})
    assert resp.status_code == 200
