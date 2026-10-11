"""
Tests for per-user API keys.

Covers: token format, generation, hashing, auth flow, CRUD routes,
rate pool classification, premium gate, MCP user context.
"""

import os
import pytest


API_KEY = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')


# ── Token format + generation ──────────────────────────────────

class TestApiKeyFormat:
    def test_generate_has_prefix(self):
        from carrac.apikeys.auth import generate_api_key, TOKEN_PREFIX
        key = generate_api_key()
        assert key.startswith(TOKEN_PREFIX)
        assert len(key) == len(TOKEN_PREFIX) + 32

    def test_validate_valid_key(self):
        from carrac.apikeys.auth import generate_api_key, validate_key_format
        assert validate_key_format(generate_api_key())

    def test_validate_rejects_invalid(self):
        from carrac.apikeys.auth import validate_key_format
        assert not validate_key_format('')
        assert not validate_key_format(None)
        assert not validate_key_format('cvx_k_tooshort')
        assert not validate_key_format('cvx_w_' + 'a' * 32)  # wrong prefix
        assert not validate_key_format('cvx_k_' + 'g' * 32)  # non-hex

    def test_hash_deterministic(self):
        from carrac.apikeys.auth import hash_api_key
        key = 'cvx_k_' + 'a' * 32
        assert hash_api_key(key) == hash_api_key(key)

    def test_suffix(self):
        from carrac.apikeys.auth import get_key_suffix
        assert get_key_suffix('cvx_k_abcd1234abcd1234abcd1234abcd1234') == '1234'


# ── Auth flow ──────────────────────────────────────────────────

class TestApiKeyAuth:
    def test_shared_key_still_works(self, app, client):
        """Shared admin key should still verify without setting g.api_key_user_id."""
        with app.test_request_context(headers={'X-Internal-Key': API_KEY}):
            from flask import g
            from carrac.auth.decorators import verify_api_key
            result = verify_api_key(API_KEY)
            assert result is True
            assert not hasattr(g, 'api_key_user_id')

    def test_shared_key_on_public_header_rejected(self, app, client):
        """The shared key on X-API-Key only (no X-Internal-Key) fails closed."""
        with app.test_request_context(headers={'X-API-Key': API_KEY}):
            from carrac.auth.decorators import verify_api_key
            assert verify_api_key(API_KEY) is False

    def test_invalid_key_rejected(self, app, client):
        """Random key should be rejected."""
        with app.test_request_context():
            from carrac.auth.decorators import verify_api_key
            assert verify_api_key('cvx_k_0000000000000000000000000000dead') is False

    def test_empty_key_rejected(self, app, client):
        with app.test_request_context():
            from carrac.auth.decorators import verify_api_key
            assert verify_api_key('') is False
            assert verify_api_key(None) is False


# ── CRUD routes ────────────────────────────────────────────────

class TestApiKeyCRUD:
    def _premium_client(self, client):
        """Set session to premium tier user, clearing any leftover keys."""
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'admin@test.com'
            sess['is_admin'] = True
            sess['tier'] = 'admin'
        # Revoke any existing keys from prior tests to avoid limit conflicts
        resp = client.get('/api/keys')
        if resp.status_code == 200:
            for k in resp.get_json().get('keys', []):
                if not k.get('revoked'):
                    client.delete(f'/api/keys/{k["id"]}')
        return client

    def _free_client(self, client):
        """Set session to free tier user, clearing any leftover keys.

        Without the cleanup, key-creating tests sharing user_id=2 accumulate
        keys across the suite and later trip the 5-active-key cap (flaky
        full-suite failure that passes in isolation).
        """
        with client.session_transaction() as sess:
            sess['user_id'] = 2
            sess['email'] = 'free@test.com'
            sess['is_admin'] = False
            sess['tier'] = 'free'
        resp = client.get('/api/keys')
        if resp.status_code == 200:
            for k in resp.get_json().get('keys', []):
                if not k.get('revoked'):
                    client.delete(f'/api/keys/{k["id"]}')
        return client

    def test_create_key_requires_auth(self, client):
        resp = client.post('/api/keys', json={'name': 'test'})
        assert resp.status_code in (302, 401)

    def test_free_tier_can_create_key(self, client):
        # Free invite-only beta: free users mint keys for MCP / the carrac TUI.
        c = self._free_client(client)
        resp = c.post('/api/keys', json={'name': 'test'})
        assert resp.status_code == 201, f'Expected 201, got {resp.status_code}: {resp.get_json()}'
        assert resp.get_json()['key'].startswith('cvx_k_')

    def test_create_key_success(self, client):
        c = self._premium_client(client)
        resp = c.post('/api/keys', json={'name': 'My Key'})
        assert resp.status_code == 201, f'Expected 201, got {resp.status_code}: {resp.get_json()}'
        data = resp.get_json()
        assert data['key'].startswith('cvx_k_')
        assert data['name'] == 'My Key'
        assert data['suffix'] == data['key'][-4:]

    def test_list_keys(self, client):
        c = self._premium_client(client)
        # Create one first
        c.post('/api/keys', json={'name': 'List Test'})
        resp = c.get('/api/keys')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'keys' in data
        # Keys should have suffix but not full key
        for k in data['keys']:
            assert 'token_suffix' in k
            assert 'token_hash' not in k

    def test_revoke_key(self, client):
        c = self._premium_client(client)
        # Create then revoke
        create_resp = c.post('/api/keys', json={'name': 'Revoke Test'})
        key_id = create_resp.get_json()['id']
        revoke_resp = c.delete(f'/api/keys/{key_id}')
        assert revoke_resp.status_code == 200
        assert revoke_resp.get_json()['success'] is True

    def test_max_5_keys_limit(self, client):
        c = self._premium_client(client)
        # Check how many active keys exist from prior tests
        existing = c.get('/api/keys').get_json()['keys']
        active = len([k for k in existing if not k.get('revoked')])
        remaining = 5 - active
        for i in range(remaining):
            resp = c.post('/api/keys', json={'name': f'Limit {i}'})
            assert resp.status_code == 201, f'Failed creating key {i+1}/{remaining}'
        # Next one should fail
        resp = c.post('/api/keys', json={'name': 'Over limit'})
        assert resp.status_code == 400
        assert 'Maximum' in resp.get_json()['error']


# ── MCP with per-user key ─────────────────────────────────────

class TestMCPUserContext:
    # shared-key reaches non-user-scoped tool: HTTP coverage now in
    # tests/fastapi/test_mcp.py::test_mcp_shared_key_reaches_non_user_scoped_tool
    # (there is no Flask /mcp blueprint).

    def test_get_mcp_user_id_helper(self, app):
        """get_mcp_user_id raises ToolError when no per-user binding
        is present. It must never fall back to 1 (admin account id), which
        would leak admin data to any shared-key caller of a user-scoped tool.

        get_mcp_caller_user_id has no flask.g fallback, to prevent stale thread-local data from
        being readable across requests on reused threads. Callers must now
        use set_mcp_caller() to supply identity via the contextvar.
        """
        from carrac.mcp.tools import get_mcp_user_id
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.runtime import set_mcp_caller, reset_mcp_caller

        # No caller set → ToolError.
        reset_mcp_caller()
        with pytest.raises(ToolError):
            get_mcp_user_id()

        # With set_mcp_caller → returns the bound user id via contextvar.
        set_mcp_caller(99, "premium")
        try:
            assert get_mcp_user_id() == 99
        finally:
            reset_mcp_caller()
