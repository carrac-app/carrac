"""Shareable Plays — END-TO-END through the strangler ASGI app.

Strangler parity: FastAPI-live write endpoints must be
exercised through ``asgi_client``, not Flask test-client or router-shape
checks — a Flask twin would give false green while production traffic hits
FastAPI. These tests authenticate exactly like a real API caller: a per-user
API key minted in the test DB, sent via ``X-API-Key`` (which also bypasses
the CSRF middleware, same as production API clients).

Covers: 401 anon, mint 201 + absolute URLs, get-or-create reuse, explicit
toggles applied on reuse, PUT toggle/refresh semantics, 402 quota with the
upgrade-modal shape, DELETE revoke → public 410 across runtimes (FastAPI
write, Flask read).
"""

from __future__ import annotations

from carrac.plays import shares as sh
from carrac.plays.models import play_create

# ``member_key`` now lives in tests/fastapi/conftest.py (shared with the
# trade-share suite).


def _play_for(uid, **kwargs):
    defaults = dict(
        title="ASGI share play",
        thesis="Strangler-tested thesis.",
        primary_asset="BTC",
        direction="long",
        conditions={"logic": "all", "rules": [
            {"type": "price_level", "met": False, "label": "Price > 100k"},
        ]},
    )
    defaults.update(kwargs)
    return play_create(uid, **defaults)


def test_mint_requires_auth(asgi_client):
    resp = asgi_client.post("/api/plays/1/share", json={})
    assert resp.status_code in (401, 302)


def test_mint_returns_absolute_share_payload(asgi_client, member_key):
    headers, uid = member_key
    play = _play_for(uid)

    resp = asgi_client.post(f"/api/plays/{play['id']}/share", json={},
                            headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["url"].startswith("http") and data["url"].endswith(f"/s/{data['slug']}")
    assert data["image_url"].endswith(f"/s/{data['slug']}/card.png")
    assert data["include_thesis"] is True
    assert data["include_outcome"] is False     # default OFF
    assert data["snapshot_status"] == "watching"


def test_mint_reuse_and_explicit_toggles_apply(asgi_client, member_key):
    headers, uid = member_key
    play = _play_for(uid)

    first = asgi_client.post(f"/api/plays/{play['id']}/share", json={},
                             headers=headers).json()
    again = asgi_client.post(f"/api/plays/{play['id']}/share",
                             json={"include_thesis": False},
                             headers=headers).json()
    assert again["slug"] == first["slug"]       # same active share
    assert again["include_thesis"] is False     # toggle applied, not ignored

    share = sh.get_share_by_slug(first["slug"])
    assert "thesis" not in share["snapshot"]


def test_put_updates_toggles_and_404_without_share(asgi_client, member_key):
    headers, uid = member_key
    play = _play_for(uid)

    # No active share yet -> 404.
    resp = asgi_client.put(f"/api/plays/{play['id']}/share",
                           json={"include_plan": False}, headers=headers)
    assert resp.status_code == 404

    asgi_client.post(f"/api/plays/{play['id']}/share", json={}, headers=headers)
    resp = asgi_client.put(f"/api/plays/{play['id']}/share",
                           json={"include_plan": False}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["include_plan"] is False


def test_share_quota_402_with_upgrade_shape(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    monkeypatch.setitem(sh.SHARE_QUOTA_BY_TIER, "member", 1)

    p1 = _play_for(uid, title="first")
    p2 = _play_for(uid, title="second")

    assert asgi_client.post(f"/api/plays/{p1['id']}/share", json={},
                            headers=headers).status_code == 201
    resp = asgi_client.post(f"/api/plays/{p2['id']}/share", json={},
                            headers=headers)
    assert resp.status_code == 402
    # The strangler's http_exception_handler flattens dict details to the
    # top level (Flask-parity shape the upgrade modal consumes).
    body = resp.json()
    assert body["limit_type"] == "share"
    assert body["current"] == 1 and body["max"] == 1
    assert "error" in body

    # Re-sharing the already-shared play stays allowed (no new quota charge).
    assert asgi_client.post(f"/api/plays/{p1['id']}/share", json={},
                            headers=headers).status_code == 201


def test_revoke_via_fastapi_gives_410_on_flask_public_page(
        asgi_client, client, member_key):
    """Cross-runtime: FastAPI DELETE -> Flask /s/<slug> serves 410."""
    headers, uid = member_key
    play = _play_for(uid)

    slug = asgi_client.post(f"/api/plays/{play['id']}/share", json={},
                            headers=headers).json()["slug"]
    assert client.get(f"/s/{slug}").status_code == 200

    resp = asgi_client.delete(f"/api/plays/{play['id']}/share", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["revoked"] == 1

    # Public reads via the Flask ``client`` (main thread, per-test DB). Both
    # /s/ (SSR) and /api/s/ (JSON) are Flask-only blueprint routes — not in any
    # MIGRATED set — so the Flask test client is the faithful path. (Reading
    # /api/s/ through asgi_client would hit the strangler's WSGI-fallthrough
    # worker thread, whose sqlite conn binds to the first asgi test's DB.)
    assert client.get(f"/s/{slug}").status_code == 410
    assert client.get(f"/api/s/{slug}").status_code == 410


def test_mint_unknown_play_404(asgi_client, member_key):
    headers, _uid = member_key
    resp = asgi_client.post("/api/plays/999999/share", json={}, headers=headers)
    assert resp.status_code == 404


def test_cannot_share_someone_elses_play(asgi_client, member_key):
    headers, uid = member_key
    other_play = _play_for(uid + 1000)   # a different user's play
    resp = asgi_client.post(f"/api/plays/{other_play['id']}/share", json={},
                            headers=headers)
    assert resp.status_code == 404       # not-owned == not-found (no oracle)
