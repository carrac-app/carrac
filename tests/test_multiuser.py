"""
Tests for multi-user features.

Covers:
- User-scoped drawing queries (user_id filtering)
- get_request_user_id helper
- Rate limit key function
"""

import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tests using the full `app` fixture require pandas (via database.py import in conftest).
# Mark them so they skip gracefully when pandas is unavailable.
try:
    import pandas  # noqa: F401
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
needs_pandas = pytest.mark.skipif(not HAS_PANDAS, reason="requires pandas (full app fixture)")


@needs_pandas
class TestDrawingUserScoping:
    """Test that drawing endpoints properly scope by user_id."""

    def test_drawings_all_requires_auth(self, client):
        """Test /api/drawings/all requires auth."""
        response = client.get('/api/drawings/all')
        assert response.status_code in [302, 401]

    def test_drawings_all_with_session(self, auth_client):
        """Test /api/drawings/all works with session auth."""
        response = auth_client.get('/api/drawings/all')
        assert response.status_code == 200
        data = response.get_json()
        assert data is not None

    def test_drawings_all_with_api_key(self, client):
        """Shared admin key has no per-user binding → 401.

        It must never return 200 with EVERY user's drawings.
        Per-user cvx_k_ keys still work — covered in test_auth_idor.py.
        """
        response = client.get('/api/drawings/all',
                              headers={'X-API-Key': 'test-api-key-for-testing'})
        assert response.status_code == 401

    def test_drawings_context_with_session(self, auth_client):
        """Test /api/drawings/context works with session auth."""
        response = auth_client.get('/api/drawings/context?assets=BTC')
        assert response.status_code == 200

    def test_drawings_context_with_api_key(self, client):
        """Shared admin key has no per-user binding → 401.

        It must never return 200 with EVERY user's drawings/scenarios.
        Per-user cvx_k_ keys still work — covered in test_auth_idor.py.
        """
        response = client.get('/api/drawings/context?assets=BTC',
                              headers={'X-API-Key': 'test-api-key-for-testing'})
        assert response.status_code == 401

    def test_scenarios_assets_with_session(self, auth_client):
        """Test /api/drawings/scenarios/assets works with session."""
        response = auth_client.get('/api/drawings/scenarios/assets')
        assert response.status_code == 200

    def test_positions_with_session(self, auth_client):
        """Test /api/drawings/positions works with session."""
        response = auth_client.get('/api/drawings/positions')
        assert response.status_code == 200


class TestContentDatabaseUserScope:
    """Test ContentDatabase user_id scoping at the DB level."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        import carrac.db_pool as _cdb
        _cdb._db_type = None
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        from carrac.content_db import ContentDatabase
        return ContentDatabase(db_path=str(tmp_path / 'test_drawings.db'))

    def test_save_with_user_id(self, db):
        """Test saving a drawing with user_id."""
        drawing_id = db.save_drawing(
            asset='BTC', timeframe='1D', drawing_type='horizontal_line',
            price_level=50000, user_id=1
        )
        assert drawing_id > 0

    def test_get_drawings_user_scoped(self, db):
        """Test user sees own + system (user_id=0) drawings."""
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=50000, user_id=0)  # system
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=55000, user_id=1)  # user 1
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=60000, user_id=2)  # user 2

        # User 1 should see own + system
        drawings = db.drawing_list(asset='BTC', user_id=1)
        price_levels = {d['price_level'] for d in drawings}
        assert 50000 in price_levels  # system
        assert 55000 in price_levels  # own
        assert 60000 not in price_levels  # other user

    def test_get_drawings_no_user_sees_all(self, db):
        """Test cross-user sentinel (-1) returns all drawings.

        ``user_id=None`` now asserts; pass ``-1`` for legitimate
        cross-user reads (system/admin code paths).
        """
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=50000, user_id=0)
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=55000, user_id=1)

        drawings = db.drawing_list(asset='BTC', user_id=-1)
        assert len(drawings) == 2

    def test_get_drawings_rejects_none_user_id(self, db):
        """Passing user_id=None must raise — defense-in-depth."""
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=50000, user_id=0)
        with pytest.raises(AssertionError):
            db.drawing_list(asset='BTC', user_id=None)
        with pytest.raises(AssertionError):
            db.drawing_list(asset='BTC', user_id=0)

    def test_get_all_drawings_grouped_user_scoped(self, db):
        """Test grouped query respects user_id."""
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=50000, user_id=0)
        db.save_drawing(asset='ETH', timeframe='1D', drawing_type='horizontal_line',
                        price_level=3000, user_id=1)
        db.save_drawing(asset='ETH', timeframe='1D', drawing_type='horizontal_line',
                        price_level=3500, user_id=2)

        grouped = db.get_all_drawings_grouped(user_id=1)
        # User 1 should see BTC (system) and ETH (own), but not user 2's ETH
        all_drawings = []
        for asset_drawings in grouped.values():
            for tf_drawings in asset_drawings.values():
                all_drawings.extend(tf_drawings)

        price_levels = {d['price_level'] for d in all_drawings}
        assert 50000 in price_levels  # system
        assert 3000 in price_levels   # user 1
        assert 3500 not in price_levels  # user 2


class TestScenarioArchive:
    """Test scenario archive toggle at DB level."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        import carrac.db_pool as _cdb
        _cdb._db_type = None
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        from carrac.content_db import ContentDatabase
        return ContentDatabase(db_path=str(tmp_path / 'test_archive.db'))

    def test_archive_via_metadata(self, db):
        """Test archiving a scenario by setting metadata.archived."""
        sid = db.save_drawing(
            asset='BTC', timeframe='1D', drawing_type='scenario',
            label='Bull case', metadata={'is_group': True, 'name': 'Bull case', 'archived': False}
        )
        # Archive it (tests use -1 sentinel for cross-user updates)
        db.drawing_update(sid, user_id=-1, metadata={'is_group': True, 'name': 'Bull case', 'archived': True})
        drawing = db.get_drawing_by_id(sid)
        assert drawing['metadata']['archived'] is True

    def test_restore_via_metadata(self, db):
        """Test restoring an archived scenario."""
        sid = db.save_drawing(
            asset='BTC', timeframe='1D', drawing_type='scenario',
            label='Bear case', metadata={'is_group': True, 'name': 'Bear case', 'archived': True}
        )
        db.drawing_update(sid, user_id=-1, metadata={'is_group': True, 'name': 'Bear case', 'archived': False})
        drawing = db.get_drawing_by_id(sid)
        assert drawing['metadata']['archived'] is False

    def test_archived_excluded_from_scenario_assets(self, db):
        """Test that archived scenarios are excluded from asset list."""
        db.save_drawing(
            asset='BTC', timeframe='1D', drawing_type='scenario',
            metadata={'is_group': True, 'name': 'Active', 'archived': False}
        )
        db.save_drawing(
            asset='ETH', timeframe='1D', drawing_type='scenario',
            metadata={'is_group': True, 'name': 'Old', 'archived': True}
        )
        # Simulate what get_scenario_assets does (cross-user sentinel for the test)
        drawings = db.drawing_list(drawing_type='scenario', user_id=-1)
        assets = set()
        for d in drawings:
            meta = d.get('metadata') or {}
            if meta.get('archived'):
                continue
            assets.add(d['asset'])
        assert 'BTC' in assets
        assert 'ETH' not in assets

    def test_scenario_preserves_timeframe(self, db):
        """Test that scenarios save and return their timeframe."""
        sid = db.save_drawing(
            asset='BTC', timeframe='4H', drawing_type='scenario',
            label='4H scenario', metadata={'name': '4H breakout'}
        )
        drawing = db.get_drawing_by_id(sid)
        assert drawing['timeframe'] == '4H'


class TestGetRequestUserId:
    """Test the get_request_user_id helper."""

    @pytest.fixture
    def mini_app(self):
        """Minimal Flask app (no pandas dependency)."""
        from flask import Flask
        app = Flask(__name__)
        app.config['SECRET_KEY'] = 'test'
        return app

    def test_returns_user_id_from_session(self, mini_app):
        """Test returns session user_id when present."""
        from carrac.drawings.routes import get_request_user_id
        with mini_app.test_request_context():
            from flask import session
            session['user_id'] = 42
            assert get_request_user_id() == 42

    def test_returns_0_for_api_key_auth(self, mini_app):
        """Test returns 0 when no session user_id (API key auth)."""
        from carrac.drawings.routes import get_request_user_id
        with mini_app.test_request_context():
            assert get_request_user_id() == 0


class TestRateLimitKey:
    """Test per-user rate limit key function."""

    @pytest.fixture
    def mini_app(self):
        """Minimal Flask app (no pandas dependency)."""
        from flask import Flask
        app = Flask(__name__)
        app.config['SECRET_KEY'] = 'test'
        return app

    def test_key_uses_session_user(self, mini_app):
        """Test rate limit key includes user_id when in session."""
        from carrac.extensions import _rate_limit_key
        with mini_app.test_request_context():
            from flask import session
            session['user_id'] = 'testuser'
            key = _rate_limit_key()
            assert 'testuser' in key

    def test_key_falls_back_to_ip(self, mini_app):
        """Test rate limit key falls back to IP when no session."""
        from carrac.extensions import _rate_limit_key
        with mini_app.test_request_context(environ_base={'REMOTE_ADDR': '10.0.0.1'}):
            key = _rate_limit_key()
            assert '10.0.0.1' in key


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
