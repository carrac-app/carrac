"""The ``@spec`` decorator documents ``min_tier`` and never enforces it.

'free' bars never gate: the free tier keeps every route it can reach.
"""


def _login(client, tier, user_id='tier-gate-user'):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['email'] = 'tier-gate@example.com'
        sess['is_admin'] = False
        sess['tier'] = tier
    return client


class TestSpecDocumentsOnly:

    def test_spec_takes_no_enforce_option(self):
        import pytest

        from carrac.openapi.spec_decorator import spec
        with pytest.raises(TypeError):
            spec(summary='probe', min_tier='member', enforce_tier=True)

    def test_declared_bar_does_not_gate_the_view(self, app):
        from carrac.openapi.spec_decorator import spec

        @spec(summary='probe', min_tier='premium')
        def view():
            return 'ok'

        with app.test_request_context('/probe'):
            from flask import session
            session['user_id'] = 'u1'
            session['tier'] = 'free'
            assert view() == 'ok'
            assert view._openapi_spec['min_tier'] == 'premium'

    def test_denial_payload_lists_tiers_at_or_above_bar(self):
        from carrac.composite.min_tiers import min_tier_denial_payload
        payload = min_tier_denial_payload('free', 'member')
        assert set(payload['required_tiers']) == {'member', 'tester', 'premium', 'admin'}
        payload = min_tier_denial_payload('member', 'premium')
        assert set(payload['required_tiers']) == {'tester', 'premium', 'admin'}


class TestFreeBarsStayOpen:

    def test_free_session_keeps_api_keys_route(self, client):
        """min_tier='free' route stays reachable for free accounts
        (API_ALLOWED_TIERS deliberately includes free for the beta)."""
        _login(client, 'free')
        resp = client.get('/api/keys')
        assert resp.status_code != 403, resp.data
