"""
Tests for the IDOR + scope sweep.

Scope:
  - alerts/routes.py user_id resolution (no more 'admin' literal)
  - drawings/positions/scenarios/context never leak across users
  - read-only API keys cannot perform write operations

Strategy:
  - Hit the request layer where possible via the test client.
  - For read-only scope enforcement, monkeypatch verify_api_key so we don't
    need a real DB-backed cvx_k_ key.
  - For content_db assertions, exercise the function directly — no Flask
    context required.
"""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Many tests need pandas (pulled in via the full app fixture).
try:
    import pandas  # noqa: F401
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
needs_pandas = pytest.mark.skipif(not HAS_PANDAS, reason="requires pandas (full app fixture)")


# ---------------------------------------------------------------------------
# content_db user-id assertions (no Flask app required).
# ---------------------------------------------------------------------------

class TestDrawingDbUserIdAssertion:
    """content_db.drawing_list / drawing_update must refuse user_id=None|0."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        import carrac.db_pool as _cdb
        _cdb._db_type = None
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        from carrac.content_db import ContentDatabase
        return ContentDatabase(db_path=str(tmp_path / 'idor_drawings.db'))

    def test_get_drawings_rejects_none(self, db):
        """user_id=None must raise (would have leaked all users)."""
        with pytest.raises(AssertionError):
            db.drawing_list(asset='BTC', user_id=None)

    def test_get_drawings_rejects_zero(self, db):
        """user_id=0 must raise (system sentinel is not a caller)."""
        with pytest.raises(AssertionError):
            db.drawing_list(asset='BTC', user_id=0)

    def test_get_drawings_accepts_real_user(self, db):
        """user_id>0 returns own + system rows only."""
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=42, user_id=7)
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=99, user_id=8)
        rows = db.drawing_list(asset='BTC', user_id=7)
        prices = {r['price_level'] for r in rows}
        assert 42 in prices
        assert 99 not in prices

    def test_get_drawings_minus_one_sentinel_returns_all(self, db):
        """user_id=-1 sentinel bypass returns every user's rows."""
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=11, user_id=1)
        db.save_drawing(asset='BTC', timeframe='1D', drawing_type='horizontal_line',
                        price_level=22, user_id=2)
        rows = db.drawing_list(asset='BTC', user_id=-1)
        assert len(rows) == 2

    def test_update_drawing_rejects_none(self, db):
        """drawing_update(user_id=None) must raise."""
        sid = db.save_drawing(asset='BTC', timeframe='1D',
                              drawing_type='horizontal_line',
                              price_level=1, user_id=1)
        with pytest.raises(AssertionError):
            db.drawing_update(sid, user_id=None, label='x')

    def test_update_drawing_minus_one_bypasses_owner_filter(self, db):
        """Admin/system update via -1 sentinel works regardless of owner."""
        sid = db.save_drawing(asset='BTC', timeframe='1D',
                              drawing_type='horizontal_line',
                              price_level=1, user_id=5)
        status, _ = db.drawing_update(sid, user_id=-1, label='admin-edited')
        assert status == db.UPDATE_OK


# ---------------------------------------------------------------------------
# Drawings API leaks (request layer). Needs full app fixture.
# ---------------------------------------------------------------------------

@needs_pandas
class TestDrawingsApiNoLeak:
    """The 5 leak sites in carrac/drawings/routes.py."""

    def test_drawings_unauth_returns_401(self, client):
        """Unauthenticated GET /api/drawings must NOT dump every user."""
        r = client.get('/api/drawings')
        # login_required_api decorator → 401 (or 302 if it falls through to redirect).
        assert r.status_code in (401, 302)

    def test_drawings_session_user_only_sees_own(self, client):
        """Session user A sees own drawings, not user B's."""
        from carrac.content_db import get_content_db
        db = get_content_db()
        # Seed two users' drawings.
        db.save_drawing(asset='BTC', timeframe='1D',
                        drawing_type='horizontal_line',
                        price_level=11111, user_id=101)
        db.save_drawing(asset='BTC', timeframe='1D',
                        drawing_type='horizontal_line',
                        price_level=22222, user_id=202)

        with client.session_transaction() as sess:
            sess['user_id'] = 101
            sess['email'] = 'a@example.com'
            sess['tier'] = 'member'

        r = client.get('/api/drawings?asset=BTC')
        assert r.status_code == 200
        prices = {d['price_level'] for d in r.get_json()['drawings']}
        assert 11111 in prices
        assert 22222 not in prices  # cross-user leak would surface here

    def test_drawings_shared_admin_key_refused(self, client):
        """Shared admin key has no per-user binding → must NOT dump everyone.

        `get_request_user_id()` resolves shared-admin-key callers
        to 0, and the handler now 401s instead of passing user_id=None to
        the DB.
        """
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        r = client.get('/api/drawings?asset=BTC', headers={'X-Internal-Key': api_key})
        # We want a 401, not a 200-with-everybody's-data.
        assert r.status_code == 401
        body = r.get_json()
        assert body['success'] is False
        assert 'Per-user authentication' in body['error']

    def test_positions_shared_admin_key_refused(self, client):
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        r = client.get('/api/drawings/positions', headers={'X-Internal-Key': api_key})
        assert r.status_code == 401

    def test_drawings_context_shared_admin_key_refused(self, client):
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        r = client.get('/api/drawings/context?assets=BTC',
                       headers={'X-Internal-Key': api_key})
        assert r.status_code == 401

    def test_scenarios_assets_shared_admin_key_refused(self, client):
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        r = client.get('/api/drawings/scenarios/assets',
                       headers={'X-Internal-Key': api_key})
        assert r.status_code == 401

    def test_drawings_all_shared_admin_key_refused(self, client):
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        r = client.get('/api/drawings/all', headers={'X-Internal-Key': api_key})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Alerts user_id resolution.
# ---------------------------------------------------------------------------

@needs_pandas
class TestAlertsUserIdResolution:
    """alerts/routes.py must never assign 'admin' literal for per-user keys."""

    def test_resolve_alert_user_id_session(self, app):
        from carrac.alerts.routes import _resolve_alert_user_id
        with app.test_request_context():
            from flask import session
            session['user_id'] = 42
            assert _resolve_alert_user_id() == '42'

    def test_resolve_alert_user_id_api_key(self, app):
        from carrac.alerts.routes import _resolve_alert_user_id
        with app.test_request_context():
            from flask import g
            g.api_key_user_id = 99
            assert _resolve_alert_user_id() == '99'

    def test_resolve_alert_user_id_none(self, app):
        from carrac.alerts.routes import _resolve_alert_user_id
        with app.test_request_context():
            assert _resolve_alert_user_id() is None

    def test_resolve_prefers_api_key_over_session(self, app):
        """If both are set (unusual), the per-user API key wins.

        Pinning session-loss safety: a per-user key explicitly identifies
        its owner, so we honour it over a stale session.
        """
        from carrac.alerts.routes import _resolve_alert_user_id
        with app.test_request_context():
            from flask import g, session
            session['user_id'] = 1
            g.api_key_user_id = 99
            assert _resolve_alert_user_id() == '99'

    def test_get_alerts_shared_admin_key_refused(self, client):
        """Shared admin key (no api_key_user_id) cannot list alerts."""
        api_key = os.environ.get('SCREENSHOT_API_KEY', 'test-api-key-for-testing')
        r = client.get('/api/alerts', headers={'X-Internal-Key': api_key})
        assert r.status_code == 401
        body = r.get_json()
        assert 'Per-user API key required' in body['error']


# ---------------------------------------------------------------------------
# Read-only API key scope enforcement.
# ---------------------------------------------------------------------------

@needs_pandas
class TestReadOnlyApiKeyScope:
    """The before_request hook must block writes when scope=='read-only'."""

    def _read_only_key(self, monkeypatch, user_id=77):
        """Monkeypatch the API-key resolution chain.

        ``rate_pools.classify_request`` delegates to the shared
        ``_classify_pure`` helper which calls
        ``carrac.auth.pure_resolvers.resolve_api_key``. The Flask-side
        ``verify_api_key`` is still used by per-route helpers, so patch
        BOTH so either code path resolves the test key.
        """
        from flask import g
        import carrac.auth.decorators as _auth
        import carrac.auth.pure_resolvers as _pure

        def _verify(api_key):
            if api_key == 'cvx_k_test_readonly':
                g.api_key_user_id = user_id
                g.api_key_tier = 'premium'
                g.api_key_scope = 'read-only'
                return True
            if api_key == 'cvx_k_test_full':
                g.api_key_user_id = user_id
                g.api_key_tier = 'premium'
                g.api_key_scope = 'full'
                return True
            return False

        def _resolve(api_key, *, allow_shared=False):
            if api_key == 'cvx_k_test_readonly':
                return {'user_id': user_id, 'tier': 'premium', 'scope': 'read-only'}
            if api_key == 'cvx_k_test_full':
                return {'user_id': user_id, 'tier': 'premium', 'scope': 'full'}
            return None

        monkeypatch.setattr(_auth, 'verify_api_key', _verify)
        monkeypatch.setattr(_pure, 'resolve_api_key', _resolve)

    def test_read_only_get_allowed(self, client, monkeypatch):
        """GET should pass through the scope hook."""
        self._read_only_key(monkeypatch)
        r = client.get('/api/alerts', headers={'X-API-Key': 'cvx_k_test_readonly'})
        # 200 (success) or 401 (auth wall) is fine — anything but 403 from
        # the scope hook. The point: GET is not blocked by the new hook.
        assert r.status_code != 403 or 'read-only' not in (r.get_json() or {}).get('error', '')

    # Writes the Flask app itself serves; FastAPI-served writes are covered
    # by tests/test_readonly_scope_asgi.py.
    @pytest.mark.parametrize('method,path,body', [
        ('POST', '/api/alerts/1/toggle', {}),
        ('POST', '/api/journal/trades/1/reopen', {}),
        ('POST', '/api/journal/trades/import', {}),
        ('DELETE', '/api/journal/screenshots/1', None),
    ])
    def test_read_only_writes_blocked(self, client, monkeypatch, method, path, body):
        """Flask write routes must 403 with read-only scope."""
        self._read_only_key(monkeypatch)
        r = client.open(path, method=method,
                        data=json.dumps(body),
                        headers={'X-API-Key': 'cvx_k_test_readonly',
                                 'Content-Type': 'application/json'})
        assert r.status_code == 403, (
            f"{method} {path} expected 403, got {r.status_code}: {r.data[:200]}"
        )
        body = r.get_json() or {}
        assert 'read-only' in body.get('error', '').lower()

    def test_full_scope_writes_pass_scope_hook(self, client, monkeypatch):
        """A full-scope key must NOT be blocked by the scope hook.

        It may still 4xx for other reasons (tier limits, validation), but
        the scope hook itself must not 403 it. We assert the response is
        not a scope-hook 403 specifically.
        """
        self._read_only_key(monkeypatch)
        r = client.post('/api/alerts/1/toggle',
                        data=json.dumps({}),
                        headers={'X-API-Key': 'cvx_k_test_full',
                                 'Content-Type': 'application/json'})
        body = r.get_json() or {}
        # The scope hook returns this exact phrasing — make sure THIS body
        # never appears for a full-scope key.
        assert 'read-only API key cannot perform write operations' not in body.get('error', '')


# ---------------------------------------------------------------------------
# Per-user API key cross-user isolation.
# ---------------------------------------------------------------------------

@needs_pandas
class TestPerUserApiKeyIsolation:
    """A per-user API key must only see / write its own rows."""

    def _bind_user(self, monkeypatch, user_id):
        """Patch verify_api_key to bind a per-user key to user_id.

        We patch BOTH the source symbol on carrac.auth.decorators (used
        by classify_request via late-bound import) AND the alias inside
        carrac.drawings.routes (which did ``from ... import
        verify_api_key`` at module import time).
        """
        from flask import g
        import carrac.auth.decorators as _auth
        import carrac.drawings.routes as _drawings_mod

        def _verify(api_key):
            if api_key and api_key.startswith('cvx_k_test_user_'):
                g.api_key_user_id = user_id
                g.api_key_tier = 'premium'
                g.api_key_scope = 'full'
                return True
            return False

        monkeypatch.setattr(_auth, 'verify_api_key', _verify)
        monkeypatch.setattr(_drawings_mod, 'verify_api_key', _verify)

    def test_per_user_key_sees_own_drawings_only(self, client, monkeypatch):
        """User B's API key must not see User A's drawings."""
        from carrac.content_db import get_content_db
        db = get_content_db()
        db.save_drawing(asset='BTC', timeframe='1D',
                        drawing_type='horizontal_line',
                        price_level=10101, user_id=101)
        db.save_drawing(asset='BTC', timeframe='1D',
                        drawing_type='horizontal_line',
                        price_level=20202, user_id=202)

        # Bind the test API key to user 202.
        self._bind_user(monkeypatch, 202)
        r = client.get('/api/drawings?asset=BTC',
                       headers={'X-API-Key': 'cvx_k_test_user_202'})
        assert r.status_code == 200, r.data
        prices = {d['price_level'] for d in r.get_json()['drawings']}
        assert 20202 in prices
        assert 10101 not in prices  # User A leak guard
