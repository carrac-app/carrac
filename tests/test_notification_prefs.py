"""Per-user notification-type prefs.

Position-health warnings are a per-user toggle (useful for some, noisy for
others). Default ON so existing behaviour is preserved; an explicit off is
respected, and omitting the key never silently disables.
"""
from carrac.alerts.routes import _validate_notification_settings, _NOTIF_DEFAULTS


def test_position_warnings_default_on():
    assert _NOTIF_DEFAULTS['position_warnings']['enabled'] is True


def test_position_warnings_absent_defaults_on():
    cleaned, err = _validate_notification_settings({})
    assert err is None
    assert cleaned['position_warnings']['enabled'] is True


def test_position_warnings_explicit_off_respected():
    cleaned, err = _validate_notification_settings({'position_warnings': {'enabled': False}})
    assert err is None
    assert cleaned['position_warnings']['enabled'] is False


def test_position_warnings_explicit_on_respected():
    cleaned, err = _validate_notification_settings({'position_warnings': {'enabled': True}})
    assert err is None
    assert cleaned['position_warnings']['enabled'] is True


class TestTelegramTokenIsVerifiedNotJustShaped:
    """Both the working and the dead token were `8178700140:...` — the same bot,
    regenerated in BotFather. No amount of pattern-matching separates them;
    only Telegram can say.
    """

    def _resp(self, code):
        class _R:
            status_code = code
        return _R()

    def test_a_revoked_token_is_rejected(self, monkeypatch):
        from carrac.alerts import routes
        import requests
        monkeypatch.setattr(requests, 'get', lambda *a, **k: self._resp(401))
        assert routes.telegram_token_rejected('8178700140:AAdead') is True

    def test_a_live_token_is_accepted(self, monkeypatch):
        from carrac.alerts import routes
        import requests
        monkeypatch.setattr(requests, 'get', lambda *a, **k: self._resp(200))
        assert routes.telegram_token_rejected('8178700140:AAlive') is False

    def test_an_unreachable_telegram_does_not_block_the_save(self, monkeypatch):
        """The asymmetry that keeps this guard from becoming an outage of its
        own: cannot-check is not the same answer as rejected."""
        from carrac.alerts import routes
        import requests

        def boom(*a, **k):
            raise ConnectionError('api.telegram.org unreachable')

        monkeypatch.setattr(requests, 'get', boom)
        assert routes.telegram_token_rejected('8178700140:AAmaybe') is None

    def test_the_shape_validator_alone_cannot_tell_them_apart(self):
        """Why the network call is needed at all — the guard this replaces
        passes the dead token happily."""
        from carrac.alerts.routes import _validate_notification_settings
        for token in ('8178700140:AAdead', '8178700140:AAlive'):
            cleaned, err = _validate_notification_settings(
                {'telegram': {'enabled': True, 'bot_token': token,
                              'chat_id': '123456789'}})
            assert err is None
            assert cleaned['telegram']['bot_token'] == token
