"""A failed preferences write is never reported as saved.

``user_db.save_preferences`` raises ``PreferencesWriteError`` when the write
fails. Handlers that save on the user's behalf answer with an error; the
two GET handlers that only migrate stored data on the side keep serving the
page and log the failure.
"""

import logging

import pytest


def _boom(*a, **k):
    raise RuntimeError('database is locked')


@pytest.fixture
def failing_write(app, monkeypatch):  # noqa: ARG001 — app binds the user_db singleton
    """The real save_preferences, run against a database that refuses the write."""
    from unittest.mock import patch

    from user_db import user_db

    real_save = user_db.save_preferences

    def _broken_save(user_id, preferences):
        with patch.object(user_db, '_tx', _boom):
            return real_save(user_id, preferences)

    monkeypatch.setattr(user_db, 'save_preferences', _broken_save)
    return user_db


# ── the primitive ───────────────────────────────────────────────────

class TestSavePreferences:
    def test_raises_when_the_write_fails(self, app, monkeypatch):  # noqa: ARG002
        from user_db import PreferencesWriteError, user_db

        uid = user_db.create_user('prefs-fail@test.dev', 'StrongPass!123', tos_accepted=True)
        monkeypatch.setattr(user_db, '_tx', _boom)
        with pytest.raises(PreferencesWriteError, match='database is locked'):
            user_db.save_preferences(uid, {'theme': 'dark'})

    def test_round_trip_still_works(self, app):  # noqa: ARG002
        from user_db import user_db

        uid = user_db.create_user('prefs-ok@test.dev', 'StrongPass!123', tos_accepted=True)
        assert user_db.save_preferences(uid, {'theme': 'dark'}) is None
        assert user_db.get_preferences(uid)['theme'] == 'dark'


# ── notification settings: the credential case ─────────────────────

class TestNotificationSettings:
    def test_failed_save_is_an_error_and_reveals_no_webhook_secret(self, failing_write, monkeypatch):
        from carrac.alerts import routes

        monkeypatch.setattr(routes, 'validate_outbound_url', lambda *a, **k: (True, '93.184.216.34'))
        monkeypatch.setattr(failing_write, 'get_preferences', lambda uid: {})
        payload, status = routes.apply_notification_settings(
            7, {'webhook': {'enabled': True, 'url': 'https://hooks.example.com/x'}},
            {'webhooks': True})
        assert status == 500
        assert 'error' in payload
        assert 'webhook_secret' not in payload
        assert payload.get('status') != 'saved'

    def test_successful_save_still_reveals_the_new_secret_once(self, app, monkeypatch):  # noqa: ARG002
        from carrac.alerts import routes
        from user_db import user_db

        uid = user_db.create_user('wh-ok@test.dev', 'StrongPass!123', tos_accepted=True)
        monkeypatch.setattr(routes, 'validate_outbound_url', lambda *a, **k: (True, '93.184.216.34'))
        payload, status = routes.apply_notification_settings(
            uid, {'webhook': {'enabled': True, 'url': 'https://hooks.example.com/x'}},
            {'webhooks': True})
        assert status == 200
        assert payload['status'] == 'saved'
        assert payload['webhook_secret']

    def test_failed_secret_rotation_does_not_return_the_secret(self, client, failing_write, monkeypatch):
        from carrac.alerts import routes

        monkeypatch.setattr(routes, 'get_request_tier', lambda: 'premium')
        monkeypatch.setattr(routes, 'get_tier_limits', lambda tier: {'webhooks': True})
        monkeypatch.setattr(failing_write, 'get_preferences', lambda uid: {})
        with client.session_transaction() as sess:
            sess['user_id'] = 7
        r = client.post('/api/notification-settings/webhook/regenerate')
        assert r.status_code == 500
        assert 'secret' not in r.get_json()
        assert 'error' in r.get_json()


# ── handlers that save on the user's behalf answer with an error ──

def test_user_me_post_reports_the_failure(app, client, failing_write, monkeypatch):
    # The exception is the error response: let Flask turn it into its 500
    # instead of re-raising it into the test, as it does in production.
    monkeypatch.setitem(app.config, 'PROPAGATE_EXCEPTIONS', False)
    monkeypatch.setattr(failing_write, 'get_preferences', lambda uid: {})
    with client.session_transaction() as sess:
        sess['user_id'] = 7
    r = client.post('/api/user/me', json={'onboarding_completed': True})
    assert r.status_code == 500
    assert (r.get_json() or {}).get('success') is not True


# ── side-effect writes inside GET handlers ─────────────────────────

def test_dashboard_still_renders_when_the_onboarding_flag_cannot_be_saved(
        client, failing_write, monkeypatch, caplog):
    uid = failing_write.create_user('dash@test.dev', 'StrongPass!123', tos_accepted=True)
    monkeypatch.setattr(failing_write, 'get_preferences', lambda u: {})
    with client.session_transaction() as sess:
        sess['user_id'] = uid
        sess['email'] = 'dash@test.dev'
    with caplog.at_level(logging.ERROR):
        r = client.get('/')
    assert r.status_code == 200
    assert 'onboarding flag not saved' in caplog.text


def test_indicator_prefs_still_served_when_the_legacy_copy_cannot_be_saved(
        client, failing_write, monkeypatch, caplog):
    from carrac.charts import routes as charts_routes

    legacy = {'indicators': {'ema_ribbon': True}, 'favourites': [], 'groups': [], 'params': {}}
    monkeypatch.setattr(charts_routes, '_load_legacy_indicator_prefs', lambda: dict(legacy))
    monkeypatch.setattr(failing_write, 'get_preferences', lambda u: {})
    with client.session_transaction() as sess:
        sess['user_id'] = 7
    with caplog.at_level(logging.ERROR):
        r = client.get('/api/indicator-prefs')
    assert r.status_code == 200
    assert r.get_json()['indicators'].get('ema_ribbon') is True
    assert 'legacy indicator prefs not copied' in caplog.text
