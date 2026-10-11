"""Slug-scoped headless render for chart shares.

Drawings never rendered in ANY headless/Playwright screenshot because
``chart_headless.html``'s live ``/api/drawings`` fetch needs per-user auth
(session or a ``cvx_k_`` per-user API key) that the Playwright render
context can't supply (it only ever sends the shared internal/admin key) —
that fetch 401s and silently falls back to ``drawings: []``.

The fix is NOT to align header names (the resolved user would be ambiguous
under an admin key — a cross-user drawing leak risk). Instead
``/chart-headless/share/<slug>`` renders the share's OWN frozen,
PII-stripped drawings (``_freeze_drawings`` output, already sitting in
``snapshot['drawings']`` at mint time) injected server-side, bypassing
``/api/drawings`` entirely.

Strategy (mirrors tests/test_play_shares.py): model layer
(``sh.create_chart_share`` / ``_render_via_screenshot_service``) called
directly; the new Flask route exercised through the plain ``client``
fixture (it — like its sibling ``/chart-headless/<asset>/<timeframe>`` —
carries no session auth, only the internal/API key).

Covers: 200 + embedded frozen-drawings JSON, unauthenticated 401, 404
(unknown slug / malformed slug / non-chart-kind share), 410 (revoked), a
no-drawings share still serving the share-mode page (empty list, not a
crash), and that ``create_chart_share`` only threads ``share_slug`` into
``_render_via_screenshot_service`` when the snapshot actually has drawings
(the no-drawings path must stay call-for-call byte-identical to
the previous behaviour).
"""

from __future__ import annotations

import json

import pytest

from carrac.plays import shares as sh

_UID = 187
_API_KEY = 'test-api-key-for-testing'  # matches conftest.py's SCREENSHOT_API_KEY override
_HEADERS = {'X-Internal-Key': _API_KEY}
_SHARE_DATA_MARKER = 'id="carrac-headless-share-data">'


def _fake_payload(symbol="BTC", timeframe="1D", n=130, base=60000.0):
    ohlcv = []
    for i in range(n):
        o = base + i * 10
        c = o + (5 if i % 2 == 0 else -5)
        ohlcv.append({
            "time": 1700000000 + i * 86400,
            "open": o, "high": max(o, c) + 8, "low": min(o, c) - 8, "close": c,
        })
    return {"ohlcv": ohlcv, "symbol": symbol.upper(), "timeframe": timeframe}


@pytest.fixture
def patch_chart_payload(monkeypatch):
    """Patch build_chart_payload at the import site used by the freezer
    (mirrors tests/fastapi/test_chart_shares_asgi.py's fixture of the same
    name — same target, duplicated here so this file has no cross-file
    fixture dependency)."""
    import carrac.charts.service as svc

    def _fake(symbol, *, mode="htf", timeframe=None, days=None):
        return _fake_payload(symbol, timeframe or "1D")

    monkeypatch.setattr(svc, "build_chart_payload", _fake)
    return _fake


def _extract_share_data(body: str) -> dict:
    start = body.index(_SHARE_DATA_MARKER) + len(_SHARE_DATA_MARKER)
    end = body.index('</script>', start)
    return json.loads(body[start:end])


def _mint_chart_share_with_drawing(app, patch_chart_payload):  # noqa: ARG001
    from carrac.content_db import get_content_db

    drawing_id = get_content_db().save_drawing(
        asset='BTC', timeframe='1D', drawing_type='horizontal_line',
        price_level=65000.0, label='Resistance', color='#26a69a',
        user_id=_UID,
    )
    share = sh.create_chart_share(_UID, 'BTC', '1D', drawing_ids=[drawing_id])
    assert share is not None
    return share, drawing_id


# ── model layer: create_chart_share -> _render_via_screenshot_service threading ──

def test_create_chart_share_threads_slug_only_when_drawings_present(
        app, patch_chart_payload, monkeypatch):
    calls = []

    def fake_render(symbol, timeframe, **kwargs):
        calls.append(dict(kwargs))
        return None

    monkeypatch.setattr(sh, '_render_via_screenshot_service', fake_render)

    # No drawings -> the no-drawings path must stay call-for-call
    # byte-identical to the previous behaviour: no share_slug kwarg AT ALL.
    share_no_drawings = sh.create_chart_share(_UID, 'ETH', '1D')
    assert share_no_drawings is not None
    assert calls[-1] == {}

    # With a real frozen drawing -> share_slug is threaded through.
    share_with_drawing, _did = _mint_chart_share_with_drawing(app, patch_chart_payload)
    assert calls[-1] == {'share_slug': share_with_drawing['slug']}


def test_render_via_screenshot_service_forwards_share_slug(monkeypatch):
    """Unit-level: the query param actually lands on the outgoing request,
    and is entirely absent when share_slug isn't given."""
    import requests as _requests_mod

    class _FakeResp:
        status_code = 200
        content = b'jpeg-bytes'

    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured['url'] = url
        captured['params'] = params
        return _FakeResp()

    monkeypatch.setattr(_requests_mod, 'get', fake_get)

    out = sh._render_via_screenshot_service('BTC', '1D', share_slug='abcd1234')
    assert out == b'jpeg-bytes'
    assert captured['params']['share_slug'] == 'abcd1234'

    captured.clear()
    sh._render_via_screenshot_service('BTC', '1D')
    assert 'share_slug' not in captured['params']


# ── route layer: /chart-headless/share/<slug> ──

def test_requires_internal_or_api_key(client, app, patch_chart_payload):
    share, _did = _mint_chart_share_with_drawing(app, patch_chart_payload)
    resp = client.get(f"/chart-headless/share/{share['slug']}")
    assert resp.status_code == 401


def test_200_embeds_frozen_drawings(client, app, patch_chart_payload):
    share, _did = _mint_chart_share_with_drawing(app, patch_chart_payload)

    resp = client.get(f"/chart-headless/share/{share['slug']}", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert _SHARE_DATA_MARKER in body

    payload = _extract_share_data(body)
    assert payload['symbol'] == 'BTC'
    assert payload['timeframe'] == '1D'
    assert len(payload['drawings']) == 1
    d = payload['drawings'][0]
    assert d['drawing_type'] == 'horizontal_line'
    assert d['price_level'] == 65000.0
    assert d['label'] == 'Resistance'
    # _freeze_drawings' PII-stripped shape: no id / user_id leak.
    assert 'id' not in d
    assert 'user_id' not in d


def test_no_drawings_share_still_200_with_empty_list(client, app, patch_chart_payload):
    share = sh.create_chart_share(_UID, 'SOL', '1D')
    assert share is not None
    assert share['snapshot']['drawings'] == []

    resp = client.get(f"/chart-headless/share/{share['slug']}", headers=_HEADERS)
    assert resp.status_code == 200
    payload = _extract_share_data(resp.get_data(as_text=True))
    assert payload['drawings'] == []
    assert payload['symbol'] == 'SOL'


def test_404_unknown_slug(client):
    resp = client.get('/chart-headless/share/zzzzzzzz', headers=_HEADERS)
    assert resp.status_code == 404


def test_404_malformed_slug(client):
    # Uppercase + too short — fails the slug regex before any DB hit.
    resp = client.get('/chart-headless/share/AB', headers=_HEADERS)
    assert resp.status_code == 404


def test_410_revoked(client, app, patch_chart_payload):
    share, _did = _mint_chart_share_with_drawing(app, patch_chart_payload)
    n = sh.revoke_share_by_slug(share['slug'], _UID)
    assert n == 1

    resp = client.get(f"/chart-headless/share/{share['slug']}", headers=_HEADERS)
    assert resp.status_code == 410


def test_404_non_chart_kind(client, app):
    """A play/trade share slug must never render as a chart-headless page —
    this route is chart-share specific."""
    play_like_share = sh._mint_share(
        'play', _UID, {'title': 'not a chart'},
        {'include_thesis': False, 'include_conditions': False,
         'include_plan': False, 'include_outcome': False},
    )
    resp = client.get(f"/chart-headless/share/{play_like_share['slug']}", headers=_HEADERS)
    assert resp.status_code == 404
