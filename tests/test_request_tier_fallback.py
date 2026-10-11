"""get_request_tier's fallback, for a request the rate classifier did not stamp.

A per-user API key resolves to its owner's tier; only the shared internal key
is the admin tier. The admin tier is what unlocks operator-private report text.
"""
from flask import g

import carrac.auth.decorators as decorators


def _verify_as_user(tier):
    def fake(key):
        g.api_key_user_id = 7
        g.api_key_tier = tier
        return True
    return fake


def test_per_user_key_gets_its_owners_tier(app, monkeypatch):
    monkeypatch.setattr(decorators, 'verify_api_key', _verify_as_user('member'))
    with app.test_request_context('/', headers={'X-API-Key': 'cvx_k_' + 'a' * 40}):
        assert not hasattr(g, 'rate_tier')
        assert decorators.get_request_tier() == 'member'


def test_shared_internal_key_is_admin(app, monkeypatch):
    monkeypatch.setattr(decorators, 'verify_api_key', lambda key: True)
    with app.test_request_context('/', headers={'X-Internal-Key': 'shared'}):
        assert decorators.get_request_tier() == 'admin'


def test_no_key_is_free(app):
    with app.test_request_context('/'):
        assert decorators.get_request_tier() == 'free'
