"""POST /api/alerts answers 201 only for an alert that was actually stored.

A write that fails must surface as a server error. Swallowing it and returning
the alert anyway tells the user they are covered by an alert that does not
exist.
"""
import asyncio

import httpx
import pytest

_BODY = {'symbol': 'BTC', 'condition': 'above', 'price': 100.0}


@pytest.fixture
def post_alert(asgi_app, api_user, monkeypatch):
    """POST /api/alerts as a member, returning the response the server sends
    (an unhandled error comes back as the 500 a real client sees)."""
    import push_notifications
    monkeypatch.setattr(push_notifications, 'user_has_notification_channel', lambda u: True)
    headers, uid = api_user('member')

    async def _post():
        transport = httpx.ASGITransport(app=asgi_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as c:
            return await c.post('/api/alerts', json=_BODY, headers=headers)

    def _send():
        return asyncio.run(_post())

    _send.user_id = uid
    return _send


def test_create_returns_201_and_stores_the_alert(post_alert):
    import push_notifications

    resp = post_alert()

    assert resp.status_code == 201, resp.text
    stored = push_notifications.get_user_alerts(str(post_alert.user_id))
    assert [a['id'] for a in stored] == [resp.json()['alert']['id']]


def test_a_failed_write_is_not_reported_as_created(post_alert, monkeypatch):
    import push_notifications
    from user_db import user_db
    # Let the alert-limit read pass, so the INSERT is what fails.
    monkeypatch.setattr(push_notifications, 'get_user_alerts', lambda *a, **k: [])
    with user_db.transaction() as conn:
        conn.cursor().execute('DROP TABLE user_alerts')

    resp = post_alert()

    assert resp.status_code == 500, (
        f'the alert was never stored, yet the API answered {resp.status_code}')
