"""Morning-report preferences through the real front door.

``/api/preferences/sync`` is served by the FastAPI app and needs a real
DB-backed user, so these use ``asgi_client`` with a member API key.
"""

from unittest.mock import patch



def test_sync_accepts_morning_report_prefs(asgi_client, member_key):
    headers, _uid = member_key
    prefs_value = {
        'sections': {'overview': True, 'crypto': True, 'macro': False},
        'voice': 'en-US-Neural2-D',
        'notifications': True,
    }
    with patch('carrac.sync.events.publish_preference_event'):
        resp = asgi_client.post(
            '/api/preferences/sync', headers=headers,
            json={'key': 'morning_report_prefs', 'value': prefs_value},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()['success'] is True


def test_sync_rejects_unknown_key(asgi_client, member_key):
    headers, _uid = member_key
    resp = asgi_client.post(
        '/api/preferences/sync', headers=headers,
        json={'key': 'unknown_key_xyz', 'value': {}},
    )
    assert resp.status_code == 400, resp.text
    assert 'Unknown preference key' in resp.text


def test_preferences_all_returns_morning_report_prefs(app, asgi_client, member_key):
    headers, uid = member_key
    prefs_value = {'voice': 'en-GB-Neural2-A', 'speed': 1.2}

    with patch('carrac.sync.events.publish_preference_event'):
        saved = asgi_client.post(
            '/api/preferences/sync', headers=headers,
            json={'key': 'morning_report_prefs', 'value': prefs_value},
        )
    assert saved.status_code == 200, saved.text

    client = app.test_client()
    with client.session_transaction() as sess:
        sess['user_id'] = uid
        sess['email'] = 'member@example.com'
        sess['tier'] = 'member'
    resp = client.get('/api/preferences/all')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'morning_report_prefs' in data
    assert data['morning_report_prefs']['voice'] == 'en-GB-Neural2-A'
    assert data['morning_report_prefs']['speed'] == 1.2
