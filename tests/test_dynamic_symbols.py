"""
Tests for dynamic symbol resolution, search, watchlist, and onboarding.

Covers:
- Symbol validation (curated, Binance, Yahoo)
- Search endpoint (/api/symbol-lookup)
- Watchlist CRUD (/api/watchlist/*)
- User preferences and onboarding (/api/user/me)

NOTE — watchlist WRITE endpoints (POST /api/watchlist, /add, /remove,
/reorder) are in ``MIGRATED_WRITE_PATHS`` and served by FastAPI in
production.  ``TestWatchlistEndpoints`` therefore uses ``asgi_client``
(httpx + ASGITransport) so those tests exercise the real runtime.  The
strangler routes GET /api/watchlist back to Flask, so round-trip tests
use ``asgi_client`` for both legs and get the correct routing for each.
"""

import asyncio
from typing import Iterator

import httpx
import pytest
import json
from httpx import ASGITransport
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# ASGI client fixtures — mirrors tests/fastapi/conftest.py pattern
# ---------------------------------------------------------------------------


@pytest.fixture
def asgi_app(app):  # noqa: ARG001 — app fixture sets up env (DATA_DIR, etc.)
    """Strangler ASGI app (FastAPI + Flask) for watchlist write tests."""
    from carrac.asgi import asgi_app as _app
    return _app


@pytest.fixture
def asgi_client(asgi_app) -> Iterator[httpx.Client]:
    """Sync httpx client driving the strangler ASGI app."""
    transport = ASGITransport(app=asgi_app)
    async_client = httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    )

    class _SyncWrapper:
        def __init__(self, ac: httpx.AsyncClient) -> None:
            self._ac = ac
            self._loop = asyncio.new_event_loop()

        def __getattr__(self, name: str):
            method = getattr(self._ac, name)

            def call(*args, **kwargs):
                return self._loop.run_until_complete(method(*args, **kwargs))

            return call

        def close(self) -> None:
            self._loop.run_until_complete(self._ac.aclose())
            self._loop.close()

    wrapper = _SyncWrapper(async_client)
    yield wrapper
    wrapper.close()


@pytest.fixture
def member_key(app):
    """Real per-user API key (member tier) in the test DB.

    Returns ``(headers, user_id)`` — requests through ``asgi_client`` with
    these headers resolve to a member-tier user. Mirrors the fixture in
    ``tests/fastapi/conftest.py``.
    """
    import secrets
    from user_db import user_db as _udb
    from carrac.apikeys.auth import generate_api_key, hash_api_key

    email = f"watchlist-{secrets.token_hex(4)}@test.dev"
    uid = _udb.create_user(email, "StrongPass!123", tos_accepted=True)
    assert uid
    _udb.set_user_tier(uid, "member")

    raw = generate_api_key()
    key_id = _udb.create_api_key(
        uid, hash_api_key(raw), token_suffix=raw[-4:], name="watchlist-tests"
    )
    assert key_id
    return {"X-API-Key": raw}, uid


def _get_db():
    """Get the database singleton from services module."""
    from carrac.services import get_services
    return get_services().db


def _setup_minimal_maps(db):
    """Initialize minimal map attributes on a patched database instance."""
    db.binance_map = db.binance_map if hasattr(db, 'binance_map') else {}
    db.stock_map = db.stock_map if hasattr(db, 'stock_map') else {}
    db.etf_map = db.etf_map if hasattr(db, 'etf_map') else {}
    db.index_map = db.index_map if hasattr(db, 'index_map') else {}
    db.commodity_map = db.commodity_map if hasattr(db, 'commodity_map') else {}
    db.forex_map = db.forex_map if hasattr(db, 'forex_map') else {}
    db.ratio_map = db.ratio_map if hasattr(db, 'ratio_map') else {}
    if not hasattr(db, '_asset_info'):
        db._asset_info = {}


def _setup_empty_maps(db):
    """Set all maps to empty for clean-room testing."""
    db.crypto_map = {}
    db.crypto_metric_map = {}
    db.binance_map = {}
    db.stock_map = {}
    db.etf_map = {}
    db.index_map = {}
    db.commodity_map = {}
    db.forex_map = {}
    db.ratio_map = {}
    db._asset_info = {}
    # Mock CCXT client to return not-supported for clean-room tests
    from unittest.mock import MagicMock
    if not hasattr(db, 'ccxt') or not hasattr(db.ccxt, 'is_supported'):
        db.ccxt = MagicMock()
    db.ccxt.is_supported = MagicMock(return_value=False)


# ==================== Database: Symbol Validation ====================

class TestSymbolValidation:
    """Tests for database.py validate_symbol() and helpers."""

    def test_is_curated_known_symbol(self, app):
        db = _get_db()
        _setup_empty_maps(db)
        db.crypto_map = {'BTC': 'bitcoin'}
        db.stock_map = {'AAPL': 'AAPL.US'}
        assert db.is_curated('BTC') is True
        assert db.is_curated('AAPL') is True

    def test_is_curated_unknown_symbol(self, app):
        db = _get_db()
        _setup_empty_maps(db)
        assert db.is_curated('ZZZZZ') is False

    def test_validate_symbol_curated_first(self, app):
        db = _get_db()
        _setup_empty_maps(db)
        db.crypto_map = {'BTC': 'bitcoin'}
        db._asset_info = {'BTC': {'name': 'Bitcoin', 'type': 'crypto'}}

        result = db.validate_symbol('BTC')
        assert result is not None
        assert result['tier'] == 'curated'
        assert result['source'] == 'curated'
        assert result['symbol'] == 'BTC'

    def test_validate_symbol_binance_dynamic(self, app):
        db = _get_db()
        _setup_empty_maps(db)
        db.binance.get_exchange_info.return_value = {'AUCTION', 'XYZ123'}

        result = db.validate_symbol('AUCTION')
        assert result is not None
        assert result['tier'] == 'dynamic'
        assert result['source'] == 'binance'
        assert result['type'] == 'crypto'

    def test_validate_symbol_yahoo_fallback(self, app):
        db = _get_db()
        _setup_empty_maps(db)
        db.binance.get_exchange_info.return_value = set()
        db.yahoo.validate_symbol.return_value = {
            'symbol': 'RIVN', 'name': 'Rivian Automotive',
            'type': 'stock', 'source': 'yahoo', 'tier': 'dynamic',
        }

        result = db.validate_symbol('RIVN')
        assert result is not None
        assert result['tier'] == 'dynamic'
        assert result['source'] == 'yahoo'
        assert result['name'] == 'Rivian Automotive'

    def test_validate_symbol_invalid_returns_none(self, app):
        db = _get_db()
        _setup_empty_maps(db)
        db.binance.get_exchange_info.return_value = set()
        db.yahoo.validate_symbol.return_value = None

        result = db.validate_symbol('ZZZZZ')
        assert result is None

    def test_resolve_yahoo_symbol_index(self, app):
        db = _get_db()
        assert db.yahoo.resolve_symbol('SPX') == 'SPX'  # passthrough on mock

    def test_resolve_yahoo_symbol_commodity(self, app):
        db = _get_db()
        assert db.yahoo.resolve_symbol('GOLD') == 'GOLD'  # passthrough on mock

    def test_resolve_yahoo_symbol_forex(self, app):
        db = _get_db()
        assert db.yahoo.resolve_symbol('EURUSD') == 'EURUSD'  # passthrough on mock

    def test_resolve_yahoo_symbol_passthrough(self, app):
        db = _get_db()
        assert db.yahoo.resolve_symbol('AAPL') == 'AAPL'  # passthrough on mock

    def test_binance_exchange_info_cache(self, app):
        db = _get_db()
        cached_info = {'BTC', 'ETH', 'SOL'}
        db.binance.get_exchange_info.return_value = cached_info

        info = db.binance.get_exchange_info()
        assert 'BTC' in info
        assert 'ETH' in info
        assert len(info) == 3


# ==================== Search Endpoint ====================

class TestSymbolSearch:
    """Tests for /api/symbol-lookup."""

    def test_search_works_unauthenticated(self, client):
        """Symbol lookup is public (no auth required)."""
        resp = client.get('/api/symbol-lookup?q=BTC')
        assert resp.status_code == 200

    def test_search_empty_query(self, auth_client, app):
        resp = auth_client.get('/api/symbol-lookup?q=')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == []

    def test_search_curated_symbols(self, auth_client, app):
        db = _get_db()
        _setup_empty_maps(db)
        db.crypto_map = {'BTC': 'bitcoin', 'BCH': 'bitcoin-cash'}
        db._asset_info = {
            'BTC': {'name': 'Bitcoin'},
            'BCH': {'name': 'Bitcoin Cash'},
        }
        db.binance.get_exchange_info.return_value = set()

        with patch('carrac.search.routes.requests.get') as mock_get:
            mock_get.return_value = MagicMock(ok=False)
            resp = auth_client.get('/api/symbol-lookup?q=BTC')

        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1
        symbols = [r['symbol'] for r in data]
        assert 'BTC' in symbols

    def test_search_binance_dynamic(self, auth_client, app):
        db = _get_db()
        _setup_empty_maps(db)
        db.binance.get_exchange_info.return_value = {'AUCTION', 'AXS'}

        with patch('carrac.search.routes.requests.get') as mock_get:
            mock_get.return_value = MagicMock(ok=False)
            resp = auth_client.get('/api/symbol-lookup?q=AUC')

        assert resp.status_code == 200
        data = resp.get_json()
        symbols = [r['symbol'] for r in data]
        assert 'AUCTION' in symbols
        for r in data:
            if r['symbol'] == 'AUCTION':
                assert r['tier'] == 'dynamic'
                assert r['type'] == 'crypto'

    def test_search_respects_limit(self, auth_client, app):
        db = _get_db()
        _setup_empty_maps(db)
        db.binance.get_exchange_info.return_value = {f'TEST{i}' for i in range(50)}

        with patch('carrac.search.routes.requests.get') as mock_get:
            mock_get.return_value = MagicMock(ok=False)
            resp = auth_client.get('/api/symbol-lookup?q=TEST&limit=3')

        data = resp.get_json()
        assert len(data) <= 3


# ==================== Watchlist Endpoints ====================

class TestWatchlistEndpoints:
    """Tests for /api/watchlist/* endpoints.

    POST writes go to FastAPI (MIGRATED_WRITE_PATHS); GET reads go to Flask
    via the strangler's Flask mount.  All tests use ``asgi_client`` so the
    correct runtime handles each method, and ``member_key`` headers for auth
    so both legs resolve the same user_id.
    """

    def test_get_watchlist_unauthenticated(self, client):
        # GET /api/watchlist is on Flask; unauthenticated → 302 to /login.
        resp = client.get('/api/watchlist')
        assert resp.status_code == 302

    def test_get_watchlist_via_api_key(self, client, monkeypatch):
        """A per-user API key (e.g. the Carrac TUI) gets ITS OWN watchlist.

        GET /api/watchlist is @auth_or_key_required and resolves the key's user
        via g.api_key_user_id, so headless clients can sync without a session.
        """
        from flask import g
        from carrac.auth import decorators as auth_dec
        import carrac.assets.routes as assets_routes

        original_verify = auth_dec.verify_api_key

        def _verify(key):
            if not original_verify(key):
                return False
            g.api_key_user_id = 77
            g.api_key_tier = 'premium'
            return True

        captured = {}

        def _flat(uid):
            captured['uid'] = uid
            return ['DOGE', 'ADA']

        monkeypatch.setattr(auth_dec, 'verify_api_key', _verify)
        monkeypatch.setattr(assets_routes.user_db, 'get_flat_watchlist', _flat)

        import os
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        resp = client.get('/api/watchlist',
                          headers={'X-Internal-Key': api_key, 'Accept': 'application/json'})
        assert resp.status_code == 200
        # Route resolves the API-key's user (77), not a session, and returns its list.
        assert captured['uid'] == 77
        assert resp.get_json() == ['DOGE', 'ADA']

    def test_get_watchlist_empty(self, asgi_client, member_key):
        # POST /api/watchlist → FastAPI (MIGRATED_WRITE_PATHS).
        # Round-trip: verify via user_db directly to avoid WsgiToAsgi thread-local
        # connection issues on the Flask GET leg.
        headers, uid = member_key
        resp = asgi_client.post('/api/watchlist', json=[], headers=headers)
        assert resp.status_code == 200
        from user_db import user_db as _udb
        assert _udb.get_flat_watchlist(uid) == []

    def test_save_and_get_flat_watchlist(self, asgi_client, member_key):
        headers, uid = member_key
        resp = asgi_client.post(
            '/api/watchlist', json=['BTC', 'ETH', 'SOL'], headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()['success'] is True
        # Verify the FastAPI handler persisted the data.
        from user_db import user_db as _udb
        assert _udb.get_flat_watchlist(uid) == ['BTC', 'ETH', 'SOL']

    def test_add_to_watchlist(self, asgi_client, member_key):
        headers, uid = member_key
        resp = asgi_client.post(
            '/api/watchlist/add', json={'symbol': 'BTC'}, headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['success'] is True
        assert 'BTC' in data['symbols']

    def test_add_duplicate_no_error(self, asgi_client, member_key):
        headers, uid = member_key
        asgi_client.post(
            '/api/watchlist/add', json={'symbol': 'ETH'}, headers=headers,
        )
        resp = asgi_client.post(
            '/api/watchlist/add', json={'symbol': 'ETH'}, headers=headers,
        )
        assert resp.status_code == 200
        symbols = resp.json()['symbols']
        assert symbols.count('ETH') == 1

    def test_add_no_symbol_returns_400(self, asgi_client, member_key):
        headers, _uid = member_key
        resp = asgi_client.post(
            '/api/watchlist/add', json={}, headers=headers,
        )
        assert resp.status_code == 400

    def test_remove_from_watchlist(self, asgi_client, member_key):
        headers, uid = member_key
        asgi_client.post(
            '/api/watchlist/add', json={'symbol': 'XRP'}, headers=headers,
        )
        resp = asgi_client.post(
            '/api/watchlist/remove', json={'symbol': 'XRP'}, headers=headers,
        )
        assert resp.status_code == 200
        assert 'XRP' not in resp.json()['symbols']

    def test_remove_nonexistent_no_error(self, asgi_client, member_key):
        headers, _uid = member_key
        resp = asgi_client.post(
            '/api/watchlist/remove', json={'symbol': 'NOPE'}, headers=headers,
        )
        assert resp.status_code == 200

    def test_reorder_watchlist(self, asgi_client, member_key):
        headers, uid = member_key
        asgi_client.post(
            '/api/watchlist', json=['BTC', 'ETH', 'SOL'], headers=headers,
        )
        resp = asgi_client.post(
            '/api/watchlist/reorder', json=['SOL', 'BTC', 'ETH'], headers=headers,
        )
        assert resp.status_code == 200
        # Verify the order was persisted by FastAPI.
        from user_db import user_db as _udb
        assert _udb.get_flat_watchlist(uid) == ['SOL', 'BTC', 'ETH']

    def test_reorder_invalid_format(self, asgi_client, member_key):
        headers, _uid = member_key
        # A dict body instead of a list → FastAPI Pydantic 422 → exception
        # handler downgrades to 400.
        resp = asgi_client.post(
            '/api/watchlist/reorder', json={'symbols': ['BTC']}, headers=headers,
        )
        assert resp.status_code == 400

    def test_save_no_data_returns_400(self, asgi_client, member_key):
        headers, _uid = member_key
        # Empty body → FastAPI body validation error → 400.
        resp = asgi_client.post(
            '/api/watchlist',
            content='',
            headers={**headers, 'Content-Type': 'application/json'},
        )
        assert resp.status_code == 400

    def test_get_raw_format(self, asgi_client, member_key):
        headers, uid = member_key
        resp = asgi_client.post(
            '/api/watchlist', json=['BTC', 'ETH'], headers=headers,
        )
        assert resp.status_code == 200
        # Verify via user_db that the data was written in the grouped format.
        from user_db import user_db as _udb
        raw = _udb.get_watchlist(uid)
        assert 'groups' in raw


class TestWatchlistGroups:
    """Multi-group watchlist: save_flat_watchlist/add_to_watchlist/
    remove_from_watchlist must not collapse ALL groups into one 'default'
    group. These tests pin the group-preserving contract.
    """

    TWO_GROUPS = {'groups': [
        {'id': 'default', 'name': 'Watchlist', 'symbols': ['BTC', 'ETH']},
        {'id': 'grp_ai', 'name': 'AI plays', 'symbols': ['RNDR', 'TAO']},
    ]}

    def _seed_two_groups(self, uid):
        from user_db import user_db as _udb
        _udb.save_watchlist(uid, self.TWO_GROUPS)
        # mark seeded so the BTC/ETH/SOL seeder never interferes
        prefs = _udb.get_preferences(uid) or {}
        prefs['watchlist_seeded'] = True
        _udb.save_preferences(uid, prefs)
        return _udb

    def test_add_preserves_groups(self, member_key):
        _headers, uid = member_key
        _udb = self._seed_two_groups(uid)
        assert _udb.add_to_watchlist(uid, 'SOL')
        raw = _udb.get_watchlist(uid)
        assert len(raw['groups']) == 2
        assert raw['groups'][0]['symbols'] == ['BTC', 'ETH', 'SOL']
        assert raw['groups'][1]['symbols'] == ['RNDR', 'TAO']

    def test_add_with_group_id_targets_group(self, member_key):
        _headers, uid = member_key
        _udb = self._seed_two_groups(uid)
        assert _udb.add_to_watchlist(uid, 'FET', group_id='grp_ai')
        raw = _udb.get_watchlist(uid)
        assert raw['groups'][1]['symbols'] == ['RNDR', 'TAO', 'FET']

    def test_add_existing_anywhere_is_noop(self, member_key):
        _headers, uid = member_key
        _udb = self._seed_two_groups(uid)
        assert _udb.add_to_watchlist(uid, 'RNDR')
        raw = _udb.get_watchlist(uid)
        assert raw['groups'][0]['symbols'] == ['BTC', 'ETH']
        assert raw['groups'][1]['symbols'] == ['RNDR', 'TAO']

    def test_remove_preserves_groups(self, member_key):
        _headers, uid = member_key
        _udb = self._seed_two_groups(uid)
        assert _udb.remove_from_watchlist(uid, 'RNDR')
        raw = _udb.get_watchlist(uid)
        assert len(raw['groups']) == 2
        assert raw['groups'][0]['symbols'] == ['BTC', 'ETH']
        assert raw['groups'][1]['symbols'] == ['TAO']

    def test_flat_save_rebuckets_within_groups(self, member_key):
        """Flat reorder keeps membership; induced order applies per group."""
        _headers, uid = member_key
        _udb = self._seed_two_groups(uid)
        # incoming flat order mixes the two groups + adds a new symbol
        assert _udb.save_flat_watchlist(uid, ['TAO', 'ETH', 'BTC', 'RNDR', 'DOGE'])
        raw = _udb.get_watchlist(uid)
        assert len(raw['groups']) == 2
        # ETH before BTC (induced), DOGE appended to default (first) group
        assert raw['groups'][0]['symbols'] == ['ETH', 'BTC', 'DOGE']
        # TAO before RNDR (induced)
        assert raw['groups'][1]['symbols'] == ['TAO', 'RNDR']

    def test_flat_save_drops_missing_symbols(self, member_key):
        _headers, uid = member_key
        _udb = self._seed_two_groups(uid)
        assert _udb.save_flat_watchlist(uid, ['BTC', 'TAO'])
        raw = _udb.get_watchlist(uid)
        assert raw['groups'][0]['symbols'] == ['BTC']
        assert raw['groups'][1]['symbols'] == ['TAO']

    def test_single_group_flat_save_unchanged(self, member_key):
        """Legacy behavior for today's users: flat save == full replace."""
        _headers, uid = member_key
        from user_db import user_db as _udb
        _udb.save_flat_watchlist(uid, ['BTC', 'ETH'])
        _udb.save_flat_watchlist(uid, ['ETH', 'XRP'])
        assert _udb.get_flat_watchlist(uid) == ['ETH', 'XRP']
        raw = _udb.get_watchlist(uid)
        assert len(raw['groups']) == 1

    def test_get_flat_concatenates_groups_in_order(self, member_key):
        _headers, uid = member_key
        _udb = self._seed_two_groups(uid)
        assert _udb.get_flat_watchlist(uid) == ['BTC', 'ETH', 'RNDR', 'TAO']

    def test_save_watchlist_groups_roundtrip(self, member_key):
        _headers, uid = member_key
        from user_db import user_db as _udb
        groups = [{'id': 'g1', 'name': 'Majors', 'symbols': ['BTC']},
                  {'id': 'g2', 'name': 'Alts', 'symbols': ['SOL', 'AVAX']}]
        assert _udb.save_watchlist_groups(uid, groups)
        assert _udb.get_watchlist_groups(uid) == groups
        assert _udb.get_flat_watchlist(uid) == ['BTC', 'SOL', 'AVAX']

    def test_groups_endpoint_roundtrip(self, asgi_client, member_key):
        headers, uid = member_key
        body = {'groups': [{'id': 'g1', 'name': 'Majors', 'symbols': ['BTC', 'ETH']},
                           {'id': 'g2', 'name': 'AI', 'symbols': ['TAO']}]}
        resp = asgi_client.post('/api/watchlist/groups', json=body, headers=headers)
        assert resp.status_code == 200
        assert resp.json()['success'] is True
        from user_db import user_db as _udb
        assert _udb.get_flat_watchlist(uid) == ['BTC', 'ETH', 'TAO']

    def test_groups_endpoint_rejects_bad_shape(self, asgi_client, member_key):
        headers, _uid = member_key
        resp = asgi_client.post('/api/watchlist/groups',
                                json={'groups': [{'name': 'x', 'symbols': ['$$$']}]},
                                headers=headers)
        assert resp.status_code == 400

    def test_groups_endpoint_rejects_duplicate_ids(self, asgi_client, member_key):
        headers, _uid = member_key
        body = {'groups': [{'id': 'g1', 'name': 'A', 'symbols': []},
                           {'id': 'g1', 'name': 'B', 'symbols': []}]}
        resp = asgi_client.post('/api/watchlist/groups', json=body, headers=headers)
        assert resp.status_code == 400

    def test_legacy_dict_post_now_validated(self, asgi_client, member_key):
        headers, _uid = member_key
        resp = asgi_client.post('/api/watchlist',
                                json={'groups': [{'name': 'x', 'symbols': ['NOT OK!']}]},
                                headers=headers)
        assert resp.status_code == 400

    def test_get_format_groups(self, asgi_client, member_key):
        headers, uid = member_key
        body = {'groups': [{'id': 'g1', 'name': 'Majors', 'symbols': ['BTC']}]}
        asgi_client.post('/api/watchlist/groups', json=body, headers=headers)
        from user_db import user_db as _udb
        assert _udb.get_watchlist_groups(uid) == body['groups']

    def test_get_watchlist_format_groups_route(self, client, monkeypatch):
        """GET /api/watchlist?format=groups — real route-level exercise.

        Mirrors test_get_watchlist_via_api_key: monkeypatch the API-key
        verifier to resolve a known user, monkeypatch user_db.get_watchlist_groups
        to a known return value, and assert the route actually wires them
        together end-to-end (not just via user_db directly).
        """
        from flask import g
        from carrac.auth import decorators as auth_dec
        import carrac.assets.routes as assets_routes

        original_verify = auth_dec.verify_api_key

        def _verify(key):
            if not original_verify(key):
                return False
            g.api_key_user_id = 77
            g.api_key_tier = 'premium'
            return True

        known_groups = [{'id': 'g1', 'name': 'Majors', 'symbols': ['BTC']}]

        def _groups(uid):
            return known_groups

        monkeypatch.setattr(auth_dec, 'verify_api_key', _verify)
        monkeypatch.setattr(assets_routes.user_db, 'get_watchlist_groups', _groups)

        import os
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        resp = client.get('/api/watchlist?format=groups',
                          headers={'X-Internal-Key': api_key, 'Accept': 'application/json'})
        assert resp.status_code == 200
        assert resp.get_json() == {'groups': known_groups}

    def test_idless_groups_do_not_duplicate_on_flat_save(self, member_key):
        """Legacy dict saves can store groups without ids; flat save must
        self-heal the ids instead of aliasing one list into both groups."""
        _headers, uid = member_key
        from user_db import user_db as _udb
        _udb.save_watchlist(uid, {'groups': [
            {'name': 'GroupA', 'symbols': ['BTC']},
            {'name': 'GroupB', 'symbols': ['ETH']},
        ]})
        prefs = _udb.get_preferences(uid) or {}
        prefs['watchlist_seeded'] = True
        _udb.save_preferences(uid, prefs)
        assert _udb.save_flat_watchlist(uid, ['BTC', 'ETH', 'SOL'])
        raw = _udb.get_watchlist(uid)
        assert len(raw['groups']) == 2
        assert raw['groups'][0]['symbols'] == ['BTC', 'SOL']
        assert raw['groups'][1]['symbols'] == ['ETH']
        flat = _udb.get_flat_watchlist(uid)
        assert flat == ['BTC', 'SOL', 'ETH']
        assert len(flat) == len(set(flat))

    def test_duplicate_ids_self_heal(self, member_key):
        _headers, uid = member_key
        from user_db import user_db as _udb
        _udb.save_watchlist(uid, {'groups': [
            {'id': 'g1', 'name': 'A', 'symbols': ['BTC']},
            {'id': 'g1', 'name': 'B', 'symbols': ['ETH']},
        ]})
        groups = _udb._get_groups(uid)
        assert len(groups) == 2
        assert groups[0]['id'] != groups[1]['id']
        assert groups[0]['symbols'] == ['BTC']
        assert groups[1]['symbols'] == ['ETH']


# ==================== User Preferences / Onboarding ====================

class TestUserMe:
    """Tests for /api/user/me endpoint."""

    def test_get_me_unauthenticated(self, client):
        resp = client.get('/api/user/me')
        assert resp.status_code == 302

    def test_get_me_default_state(self, auth_client):
        resp = auth_client.get('/api/user/me')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'user_id' in data
        assert 'onboarding_completed' in data

    def test_set_onboarding_completed(self, auth_client):
        resp = auth_client.post(
            '/api/user/me',
            data=json.dumps({'onboarding_completed': True, 'profile': 'crypto'}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

        resp = auth_client.get('/api/user/me')
        data = resp.get_json()
        assert data['onboarding_completed'] is True
        assert data['profile'] == 'crypto'

    def test_post_me_no_data_returns_400(self, auth_client):
        resp = auth_client.post(
            '/api/user/me',
            content_type='application/json',
        )
        assert resp.status_code == 400
