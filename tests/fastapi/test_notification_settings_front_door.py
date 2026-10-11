"""Settings → Notifications must save through the front door.

POST /api/notification-settings is served by FastAPI (MIGRATED_WRITE_PATHS),
not by the Flask handler the older tests exercise. The page sends a
`webhook` block and a `position_warnings` block; a request model that forbids
them makes every Save and every
Test on that page answered 400 "webhook: Extra inputs are not permitted"
(reproduced against the live page).
Behind that, the FastAPI handler had drifted from the Flask one: it
overwrote the stored notifications wholesale (dropping the server-managed
webhook secret), and never asked Telegram whether a token is live
(the Flask handler already did). These tests drive the real ASGI app with
the exact payload templates/settings.html sends.
"""

import pytest

from user_db import user_db


def _page_payload(**overrides):
    """What settings.html gatherData() posts, field for field."""
    payload = {
        'ntfy': {'enabled': True, 'url': 'https://ntfy.sh/carrac-test-topic'},
        'discord': {'enabled': False, 'webhook_url': ''},
        'telegram': {'enabled': False, 'bot_token': '', 'chat_id': ''},
        'email': {'enabled': True},
        'webhook': {'enabled': False, 'url': ''},
        'position_warnings': {'enabled': False},
    }
    payload.update(overrides)
    return payload


def test_the_settings_page_payload_is_accepted_and_stored(asgi_client, member_key):
    headers, uid = member_key

    resp = asgi_client.post('/api/notification-settings', json=_page_payload(), headers=headers)

    assert resp.status_code == 200, resp.text
    notif = user_db.get_preferences(uid)['notifications']
    assert notif['ntfy'] == {'enabled': True, 'url': 'https://ntfy.sh/carrac-test-topic'}
    assert notif['position_warnings'] == {'enabled': False}, 'the toggle the page sends must persist'


def test_the_front_door_keeps_the_webhook_secret_it_did_not_receive(asgi_client, member_key):
    """The secret is server-managed: the page never sends it, so
    a save must carry the stored one forward, not blank it."""
    headers, uid = member_key
    prefs = user_db.get_preferences(uid)
    prefs['notifications'] = {
        'webhook': {'enabled': False, 'url': '', 'secret': 'enc:keep-me',
                    'consecutive_failures': 3, 'last_delivery_status': 'ok',
                    'last_delivery_at': '2026-09-01T00:00:00Z'},
    }
    user_db.save_preferences(uid, prefs)

    resp = asgi_client.post('/api/notification-settings', json=_page_payload(), headers=headers)

    assert resp.status_code == 200, resp.text
    wh = user_db.get_preferences(uid)['notifications']['webhook']
    assert wh['secret'] == 'enc:keep-me'
    assert wh['consecutive_failures'] == 3


def test_the_front_door_refuses_a_token_telegram_rejects(asgi_client, member_key, monkeypatch):
    """The Telegram token liveness check lives in the Flask handler; the front door must apply it too."""
    import carrac.alerts.routes as flask_routes
    monkeypatch.setattr(flask_routes, 'telegram_token_rejected', lambda token: True)
    headers, uid = member_key
    payload = _page_payload(telegram={'enabled': True, 'bot_token': '123456:ABCdefGHIjkl', 'chat_id': '42'})

    resp = asgi_client.post('/api/notification-settings', json=payload, headers=headers)

    assert resp.status_code == 400, resp.text
    assert 'rejected' in resp.json().get('error', '').lower()
    assert 'telegram' not in (user_db.get_preferences(uid).get('notifications') or {}), \
        'a rejected token must not be stored looking healthy'


def test_request_model_accepts_the_two_blocks_the_page_sends():
    from carrac.fastapi_app.models.alerts import NotificationSettingsRequest
    body = NotificationSettingsRequest(**_page_payload())
    assert body.webhook.enabled is False
    assert body.position_warnings.enabled is False


def test_request_model_still_rejects_unknown_fields():
    from pydantic import ValidationError
    from carrac.fastapi_app.models.alerts import NotificationSettingsRequest
    with pytest.raises(ValidationError):
        NotificationSettingsRequest(**_page_payload(), evil_field='injection')


def test_saving_with_the_masked_token_the_page_echoes_keeps_the_stored_one(asgi_client, member_key, monkeypatch):
    """GET /api/notification-settings shows the bot token as its first four
    characters plus '****'; the page puts that in the input and
    sends it straight back on Save. The validator then rejected the mask as
    a malformed token — so a user
    with Telegram configured could never save anything on the page."""
    from carrac.secret_box import encrypt_token, decrypt_token
    import carrac.alerts.routes as flask_routes

    headers, uid = member_key
    real = '123456:ABCdefGHIjkl-mnop'
    prefs = user_db.get_preferences(uid)
    prefs['notifications'] = {'telegram': {'enabled': True, 'bot_token': encrypt_token(real), 'chat_id': '42'}}
    user_db.save_preferences(uid, prefs)

    def _must_not_be_asked(token):
        raise AssertionError('an unchanged token must not be re-checked with Telegram on every save')
    monkeypatch.setattr(flask_routes, 'telegram_token_rejected', _must_not_be_asked)

    payload = _page_payload(telegram={'enabled': True, 'bot_token': '1234****', 'chat_id': '42'})
    resp = asgi_client.post('/api/notification-settings', json=payload, headers=headers)

    assert resp.status_code == 200, resp.text
    stored = user_db.get_preferences(uid)['notifications']['telegram']
    assert decrypt_token(stored['bot_token']) == real, 'the mask must never replace the real token'
    assert stored['chat_id'] == '42'
