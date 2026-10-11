"""PUT /api/alerts/{id} accepts every editable field create accepts, ``note`` included."""
import pytest


@pytest.fixture
def alert(asgi_client, member_key, monkeypatch):
    import push_notifications
    monkeypatch.setattr(push_notifications, 'user_has_notification_channel', lambda u: True)
    headers, _uid = member_key
    resp = asgi_client.post(
        '/api/alerts',
        json={'symbol': 'BTC', 'condition': 'above', 'price': 100.0, 'note': 'first'},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return headers, resp.json()['alert']['id']


def test_note_can_be_edited(asgi_client, alert):
    headers, alert_id = alert

    resp = asgi_client.put(f'/api/alerts/{alert_id}', json={'note': 'second'}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()['alert']['note'] == 'second'


def test_unknown_field_is_still_rejected(asgi_client, alert):
    headers, alert_id = alert

    resp = asgi_client.put(f'/api/alerts/{alert_id}', json={'colour': 'red'}, headers=headers)

    assert resp.status_code == 400
