"""Drawing writes are per-user: an anonymous caller and the shared internal
key (which carries no user) are both refused, so nothing is ever written as
user 0."""

from __future__ import annotations

import os

import pytest

WRITES = [
    ("PUT", "/api/drawings/1", {"price_level": 50000}),
    ("DELETE", "/api/drawings/1", None),
    ("POST", "/api/drawings/bulk", {"drawings": [], "replace": True}),
]


@pytest.mark.parametrize("method,path,body", WRITES)
def test_anonymous_write_is_refused(asgi_client, method, path, body):
    resp = asgi_client.request(method, path, json=body, follow_redirects=False)
    assert resp.status_code in (401, 302), resp.text


@pytest.mark.parametrize("method,path,body", WRITES)
def test_shared_internal_key_write_is_refused(asgi_client, method, path, body):
    headers = {"X-Internal-Key": os.environ["SCREENSHOT_API_KEY"]}
    resp = asgi_client.request(method, path, json=body, headers=headers,
                               follow_redirects=False)
    assert resp.status_code == 401, resp.text


def test_per_user_key_bulk_write_is_accepted(key_client):
    resp = key_client("member").post("/api/drawings/bulk", json={"drawings": []})
    assert resp.status_code in (200, 201, 207), resp.text
