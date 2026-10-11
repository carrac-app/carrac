"""A drawing created with an API key can be read back by id with that key."""

from __future__ import annotations

import pytest


@pytest.fixture
def _client(asgi_client, member_key):
    headers, uid = member_key
    return asgi_client, headers, uid


def _create(client, headers):
    resp = client.post(
        "/api/drawings",
        json={"asset": "ETHUSDT", "timeframe": "1D",
              "drawing_type": "horizontal_line", "price_level": 2500.0,
              "color": "#26a69a"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["drawing_id"]


def test_get_by_id_with_api_key(_client):
    client, headers, _uid = _client
    did = _create(client, headers)
    resp = client.get(f"/api/drawings/{did}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["drawing"]["id"] == did


def test_get_by_id_is_scoped_to_the_key_owner(asgi_client, api_user):
    owner_headers, _ = api_user("member")
    other_headers, _ = api_user("member")
    did = _create(asgi_client, owner_headers)
    assert asgi_client.get(f"/api/drawings/{did}", headers=other_headers).status_code == 404
