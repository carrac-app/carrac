"""Seam 5 — the MCP router honours the shared key only via
X-Internal-Key. Companion to tests/test_shared_key_header_split.py (seams
1-4); see its module docstring for the full rule and why every seam matters.

The router must not read the shared key from X-API-Key/Bearer: the shared
key maps to admin tier, unmetered, so POST /mcp initialize with the shared
key on X-API-Key must not return 200.
"""

SHARED = 'test-api-key-for-testing'  # forced by tests/conftest.py

_INIT = {
    'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
    'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
               'clientInfo': {'name': 'c420-test', 'version': '0'}},
}


class TestMcpRouterSeam:
    def test_shared_key_via_public_header_is_401(self, asgi_client):
        resp = asgi_client.post('/mcp', json=_INIT,
                                headers={'X-API-Key': SHARED})
        assert resp.status_code == 401

    def test_shared_key_via_internal_header_works(self, asgi_client):
        resp = asgi_client.post('/mcp', json=_INIT,
                                headers={'X-Internal-Key': SHARED})
        assert resp.status_code == 200
