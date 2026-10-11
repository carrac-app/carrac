"""
Pytest configuration and shared fixtures for the test suite.
"""

import pytest
import os
import sys
import shutil
import tempfile
import warnings
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Seal the suite from the developer's environment BEFORE anything imports
# carrac. carrac/config.py would otherwise read ./.env and fill in any key the
# shell lacks; one optional key (say FRED_API_KEY) is enough to send tests to
# live providers. Skip the file, then drop every provider/secret variable the
# suite must never see. Fake values the suite wants are set further down.
os.environ['CARRAC_SKIP_DOTENV'] = '1'
_FORBIDDEN_ENV_EXACT = (
    'FRED_API_KEY', 'FINNHUB_API_KEY', 'ANTHROPIC_API_KEY', 'AI_API_KEY',
    'SENTRY_DSN', 'SENTRY_BROWSER_DSN', 'TELEGRAM_BOT_TOKEN',
    'SCREENSHOT_API_KEY', 'TELEGRAM_TOKEN_ENCRYPTION_KEY',
)
_FORBIDDEN_ENV_PREFIX = ('BREVO_SMTP_', 'PADDLE_', 'VAPID_', 'NTFY_')
for _k in list(os.environ):
    if _k in _FORBIDDEN_ENV_EXACT or _k.startswith(_FORBIDDEN_ENV_PREFIX):
        del os.environ[_k]

# Set up test environment variables before importing any modules
# Default: force SQLite (no psycopg2 needed locally). The dedicated Postgres
# portability job (tests/pg/, .github/workflows/test.yml :: pg-portability)
# sets CARRAC_TEST_DB=postgres + DATABASE_URL to exercise the real dialect
# against the true production schema; everything else stays on SQLite exactly as
# before (CARRAC_TEST_DB unset → sqlite).
if os.environ.get('CARRAC_TEST_DB', '').lower() in ('postgres', 'postgresql'):
    os.environ['DB_TYPE'] = 'postgresql'
else:
    os.environ['DB_TYPE'] = 'sqlite'
os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-testing-only-change-in-prod')
os.environ['SCREENSHOT_API_KEY'] = 'test-api-key-for-testing'  # force: tests must not inherit a real ambient key (setdefault no-ops when the shell profile exports real credentials)
os.environ.setdefault('EMBED_API_TOKEN', 'test-embed-api-token')
os.environ.setdefault('TESTING', 'true')

# No test may ever reach a REAL notification endpoint.
#
# Layer 1 of 2 — neutralise the destination before any app module reads it.
# Both of these are module-level constants captured at import
# (carrac/notifications/ntfy.py :: NTFY_BASE, morning_engine/config.py ::
# NTFY_TOPIC / TELEGRAM_*). NTFY_TOPIC defaults to "" (no topic), but the
# suite may run from a shell that has sourced a local env file supplying the
# real topics and Telegram credentials. Forced, not setdefault, for exactly
# that reason. 127.0.0.1:9 is the discard port.
# Layer 2 (the _no_real_notifications autouse fixture below) makes an attempt
# LOUD; this layer makes it HARMLESS even if layer 2 is bypassed.
os.environ['NTFY_BASE'] = 'http://127.0.0.1:9'
os.environ['NTFY_TOPIC'] = 'carrac-test-topic-nobody-subscribes-to'
# Carrac routes by audience now (ops vs market vs infrastructure), so neutralising
# NTFY_TOPIC alone leaves two live destinations open. A local env file may supply
# the real values to the shell the suite runs from.
os.environ['NTFY_CARRAC_TOPIC'] = 'carrac-test-carrac-topic-nobody-subscribes-to'
os.environ['NTFY_MARKET_TOPIC'] = 'carrac-test-market-topic-nobody-subscribes-to'
os.environ['TELEGRAM_BOT_TOKEN'] = ''
os.environ['TELEGRAM_CHAT_ID'] = ''

# SAFETY NET: pin DATA_DIR to a throwaway temp dir BEFORE any app module is
# imported. The user_db singleton freezes its db_path from DATA_DIR at import
# time; if DATA_DIR is unset it would bind to the real ./data/users.db and
# every test that touches user_db would read/write the dev database (and leave
# residue between runs). Each xdist worker imports conftest separately, so each
# gets its own isolated dir. Per-test isolation is layered on top via the
# `app` fixture's rebind_user_db() call. force=True so a stale ambient DATA_DIR
# from the shell profile can never point the suite at real data.
_SESSION_DATA_DIR = tempfile.mkdtemp(prefix='carrac-test-data-')
os.environ['DATA_DIR'] = _SESSION_DATA_DIR

# Flask-Session's filesystem store defaults to ./flask_session in the working
# directory, which a test run would otherwise leave behind in the repository.
# Point it at the same throwaway dir. Config classes are read by create_app(),
# so set it on the base class (TestConfig inherits it) before any app is built.
from carrac.config import Config as _Config  # noqa: E402

_Config.SESSION_FILE_DIR = os.path.join(_SESSION_DATA_DIR, 'flask_session')


def pytest_unconfigure(config):
    """Remove the session-wide throwaway DATA_DIR created at import time."""
    try:
        shutil.rmtree(_SESSION_DATA_DIR, ignore_errors=True)
    except Exception:
        pass


def rebind_user_db(data_dir):
    """Rebind the process-wide user_db singleton to an isolated SQLite DB.

    The singleton (`from user_db import user_db`) caches BOTH its db_path
    (frozen in __init__ from DATA_DIR at import time) and a thread-local
    sqlite connection. Setting DATA_DIR alone therefore does NOT isolate it —
    without this rebind, every test shares one users.db, which pollutes the
    real dev DB and makes count/dedup assertions (e.g. the waitlist tests)
    flake depending on xdist worker ordering. This points the singleton at the
    given dir, drops the cached connection, and re-creates the schema.
    """
    from carrac import db_pool as _db_pool
    _db_pool._db_type = None  # re-read DB_TYPE env

    # Drop db_pool's own sqlite thread-local connection (used by code paths
    # that go through db_pool.get_conn() rather than the user_db singleton).
    _sl = getattr(_db_pool, '_sqlite_local', None)
    if _sl is not None and getattr(_sl, 'conn', None) is not None:
        try:
            _sl.conn.close()
        except Exception:
            pass
        _sl.conn = None

    from user_db import user_db as _udb
    if getattr(_udb._local, 'conn', None) is not None:
        try:
            _udb._local.conn.close()
        except Exception:
            pass
        _udb._local.conn = None
    if hasattr(_udb._local, 'pg_conn'):
        _udb._local.pg_conn = None
    _udb.db_path = Path(data_dir) / 'users.db'
    _udb.db_path.parent.mkdir(parents=True, exist_ok=True)
    _udb._init_database()
    return _udb


@pytest.fixture
def rebind_db():
    """Expose rebind_user_db to test modules (conftest isn't importable by name).

    Lets a test point the user_db singleton at its own tmp dir, e.g.:
        def fresh_db(tmp_path, rebind_db):
            return rebind_db(tmp_path / 'db')
    """
    return rebind_user_db


@pytest.fixture(autouse=True)
def _preserve_user_db_singleton():
    """Heal the process-wide user_db singleton if a test rebinds it.

    A few tests (test_user_db_real.py, test_ownership_and_prompt_injection_guards.py) call
    importlib.reload(user_db) to refresh module-level config like ADMIN_EMAIL.
    Reload re-executes the module body, which re-runs `user_db = UserDatabase()`
    — rebinding the module-level singleton to a BRAND-NEW instance. But modules
    that did `from user_db import user_db` at import (e.g. carrac.billing.routes)
    still reference the ORIGINAL instance. The result is a split brain: a later
    test's fresh_db/rebind points the *new* singleton at its tmp DB and writes
    there, while the billing resolver reads the *old* instance's DB — so e.g.
    test_full_refund_downgrades_tier intermittently fails with "could not
    resolve user". This autouse fixture snapshots the singleton before each test
    and restores it after, so a reload in one test can't poison later ones.

    The reload rebinds every module-level name, not only the singleton: an
    exception class such as ``PreferencesWriteError`` becomes a new class, and a
    module that imported the old one (``carrac.alerts.routes``) no longer
    catches what ``save_preferences`` raises. So the whole namespace is put back.
    """
    import user_db as _ud
    snapshot = dict(vars(_ud))
    yield
    if any(vars(_ud).get(k) is not v for k, v in snapshot.items()):
        vars(_ud).update(snapshot)


# ── Notification containment ──────────────────────────────────
#
# Layer 2. A test that fires a real operator alert would be INVISIBLE: a test
# that pushed a fake "General Report BLOCKED" to the live ntfy topic would
# still report all green. That alert means the free public briefing was
# dropped for a compliance finding — it has to be trustworthy, and test noise
# makes it indistinguishable from the real thing.
#
# So: every named sender is replaced with a recorder, and a test that records
# anything FAILS unless it declared the intent by requesting the `notifications`
# fixture. Silence is no longer the same as safety.
#
# The two eager targets are the canonical definitions. Patching them also
# covers every LATER `from carrac.notifications import send_ntfy` (the common
# lazy-import idiom here), because the attribute they'd read is already the
# recorder. The lazy targets are modules that bound the name at import time
# (`from ... import send_telegram_message` at module scope) and so hold their
# own reference the eager patch cannot reach — they are patched only if already
# imported, which is sufficient: if they are not imported, they cannot send.
#
# Adding a sender? Add it here. `rg -n "def send_|requests\.post" carrac/notifications/`
_NOTIFY_TARGETS_EAGER = (
    ('carrac.notifications.ntfy', 'send_ntfy'),
    ('carrac.notifications.telegram', 'send_telegram_message'),
    ('carrac.notifications', 'send_ntfy'),
    ('carrac.notifications', 'send_telegram_message'),
)

_NOTIFY_TARGETS_LAZY = (
    ('carrac.billing.routes', 'send_telegram_message'),
    ('carrac.reports.routes', 'send_telegram_message'),
    ('carrac.fastapi_app.routers.billing_paddle', 'send_telegram_message'),
    ('morning_engine.notify', 'notify'),
    ('morning_engine.engine', '_notify'),
    # User-configured webhook fan-out — a per-user endpoint, but still the
    # open internet from a test process.
    ('push_notifications', '_send_ntfy_to_url'),
    ('push_notifications', '_send_discord_alert'),
)

# Real functions, captured the first time each target is patched (i.e. before
# any replacement exists). `real_notification_senders` hands them back.
_NOTIFY_ORIGINALS = {}


@pytest.fixture(autouse=True)
def _no_real_notifications(request, monkeypatch):
    """Replace every notification sender with a recorder; fail on any send.

    Opt in with the `notifications` fixture when a test MEANS to send — it
    yields the recorded list so the send becomes an assertion instead of a
    side effect. Opt out with `real_notification_senders` when the test is
    exercising a sender's own internals (it must mock its transport itself).
    """
    import importlib
    import sys

    sent = []

    def _record(_name):
        def _recorder(*args, **kwargs):
            sent.append({'sender': _name, 'args': args, 'kwargs': kwargs})
            return True  # send_ntfy's contract; harmless for the None-returners
        return _recorder

    for mod_path, attr in _NOTIFY_TARGETS_EAGER:
        mod = importlib.import_module(mod_path)
        key = f'{mod_path}.{attr}'
        _NOTIFY_ORIGINALS.setdefault(key, getattr(mod, attr))
        monkeypatch.setattr(mod, attr, _record(key), raising=True)

    for mod_path, attr in _NOTIFY_TARGETS_LAZY:
        mod = sys.modules.get(mod_path)
        if mod is not None and hasattr(mod, attr):
            key = f'{mod_path}.{attr}'
            _NOTIFY_ORIGINALS.setdefault(key, getattr(mod, attr))
            monkeypatch.setattr(mod, attr, _record(key), raising=True)

    yield sent

    if sent and 'notifications' not in request.fixturenames:
        lines = '\n'.join(
            f"  {s['sender']}({', '.join(repr(a) for a in s['args'])}"
            f"{', ' if s['args'] and s['kwargs'] else ''}"
            f"{', '.join(f'{k}={v!r}' for k, v in s['kwargs'].items())})"
            for s in sent
        )
        pytest.fail(
            f"this test attempted {len(sent)} real notification "
            f"send(s). In prod these reach the operator's phone.\n{lines}\n"
            "Stub the sender, or request the `notifications` fixture and assert "
            "on it if the send is the thing under test."
        )


@pytest.fixture
def notifications(_no_real_notifications):
    """Opt in to notification sends, and inspect them.

    Returns the list the autouse recorder fills — each entry is
    ``{'sender': 'module.attr', 'args': (...), 'kwargs': {...}}``. Requesting
    this fixture is what stops the notification guard from failing the test, so
    a send is only ever allowed where a test says out loud that it wants one.
    """
    return _no_real_notifications


@pytest.fixture
def real_notification_senders(_no_real_notifications, monkeypatch):
    """Restore the genuine senders for tests of a sender's own internals.

    Only for tests that mock their own transport (`patch("requests.post")`) —
    e.g. tests/test_breaker_notify.py::TestNtfyHeaderEncoding, which asserts on
    the HEADERS send_ntfy builds and so needs the real function. Layer 1 stands
    behind this: NTFY_BASE points at the discard port and the Telegram
    credentials are blank, so a slip cannot reach a real endpoint.

    Both target lists must be restored: restoring only _NOTIFY_TARGETS_EAGER
    would give a test of push_notifications._send_ntfy_to_url's own headers —
    a LAZY target — the recorder back, and it would fail the teardown guard no
    matter what it asked for.
    """
    import importlib
    import sys

    for mod_path, attr in _NOTIFY_TARGETS_EAGER:
        real = _NOTIFY_ORIGINALS.get(f'{mod_path}.{attr}')
        if real is not None:
            monkeypatch.setattr(importlib.import_module(mod_path), attr, real)

    for mod_path, attr in _NOTIFY_TARGETS_LAZY:
        real = _NOTIFY_ORIGINALS.get(f'{mod_path}.{attr}')
        mod = sys.modules.get(mod_path)
        if real is not None and mod is not None:
            monkeypatch.setattr(mod, attr, real)


# ── No test may make a real LLM call ───────────────────────────
#
# Found by the same sweep as the notification guard above. The report-generation tests fake the
# Sonnet/Opus provider, but the Haiku grounding verifier builds its OWN client
# when none is injected (carrac/grounding/verify.py::verify_against_source), so 11 real POSTs
# to api.anthropic.com left the suite on every run. Locally ANTHROPIC_API_KEY is
# unset, so they 401 and the tests stay green — which is exactly why nobody saw
# it. On the deployment host, or any shell that has sourced the key, `make test` spends real
# money and a deploy is coupled to Anthropic being up.
#
# The guard sits at the TRANSPORT and matches on host, so a test that mocks its
# own requests.post is unaffected (its patch simply replaces this one) while a
# test that forgot to inject a fake client is caught. It raises a ConnectionError
# subclass so each provider's normal network-failure handling runs unchanged —
# the teardown check below is what makes it loud rather than the exception.
_LLM_HOSTS = (
    'api.anthropic.com',
    'api.x.ai',
    'api.openai.com',
    'generativelanguage.googleapis.com',
    'api.deepseek.com',
)


import requests as _requests  # noqa: E402  (after the env setup above, by design)


class RealLLMCallBlocked(_requests.exceptions.ConnectionError):
    """Raised in place of a real LLM API call.

    A ConnectionError subclass on purpose: each provider already handles a
    network failure (`except requests.RequestException` → LLMError, or the
    fail-open path in verify_against_source), so blocking the call exercises
    the SAME branch a real outage would. The teardown check is what makes the
    attempt visible; the exception type is chosen to disturb nothing.
    """


@pytest.fixture(autouse=True)
def _no_live_network(request, monkeypatch):
    """Refuse every non-loopback HTTP connection at the urllib3 pool level.

    Unblocked, the suite fires ~1,440 full Binance ticker-book
    requests (weight 80 each) plus as many CoinPaprika calls per run — one per
    create_app() — which gets the CI host's IP weight-banned by Binance.
    Nothing in the suite needs the network, so this refuses at the transport
    instead of trusting every fetcher to be mocked. Raises requests.ConnectionError so a
    provider's normal failure handling runs. Opt out with the `live_network`
    fixture if a test genuinely means to reach the internet.
    """
    if 'live_network' in request.fixturenames:
        yield
        return
    import requests
    import urllib3.connectionpool as _pool

    real_urlopen = _pool.HTTPConnectionPool.urlopen

    def _guarded_urlopen(self, method, url, *a, **kw):
        host = (self.host or '').strip('[]')
        if host in ('127.0.0.1', 'localhost', '::1', '0.0.0.0') or host.startswith('127.'):
            return real_urlopen(self, method, url, *a, **kw)
        raise requests.ConnectionError(
            f"Blocked live network call to {self.scheme}://{host}{url}. "
            "The test suite must not reach the internet — mock the fetcher, or "
            "request the `live_network` fixture if the call is the point."
        )

    monkeypatch.setattr(_pool.HTTPConnectionPool, 'urlopen', _guarded_urlopen)
    yield


@pytest.fixture
def live_network():
    """Escape hatch for _no_live_network — see the live-network guard above."""
    return True


@pytest.fixture(autouse=True)
def _no_real_llm_calls(request, monkeypatch):
    """Block outbound LLM API traffic; fail the test that attempted it.

    Opt in with the `llm_calls` fixture if a test genuinely means to reach a
    provider (nothing in the suite should, but the escape hatch keeps the guard
    honest rather than tempting someone to delete it).
    """
    import requests

    calls = []

    real_post = requests.post
    real_session_post = requests.sessions.Session.post

    def _is_llm(url):
        u = str(url)
        return any(h in u for h in _LLM_HOSTS)

    def _blocked(url):
        calls.append(str(url))
        raise RealLLMCallBlocked(
            f"Blocked a real LLM call to {url}. Inject a fake "
            "client instead of letting the provider build its own."
        )

    def _post(url, *a, **kw):
        return _blocked(url) if _is_llm(url) else real_post(url, *a, **kw)

    def _session_post(self, url, *a, **kw):
        return _blocked(url) if _is_llm(url) else real_session_post(self, url, *a, **kw)

    monkeypatch.setattr(requests, 'post', _post)
    monkeypatch.setattr(requests.sessions.Session, 'post', _session_post)

    # carrac/llm/anthropic.py goes through the SDK (httpx), not requests, so the
    # transport patch above cannot see it. It already raises when no key is set;
    # this makes it raise even when one IS set.
    import carrac.llm.anthropic as _anthropic_mod

    def _blocked_sdk(*a, **kw):
        return _blocked('https://api.anthropic.com (SDK client)')

    monkeypatch.setattr(_anthropic_mod, '_get_sdk_client', _blocked_sdk)

    yield calls

    if calls and 'llm_calls' not in request.fixturenames:
        pytest.fail(
            f"This test made {len(calls)} real LLM API call(s): "
            f"{calls}. On a box with the key set these are billed, and they "
            "couple the suite to a provider being up. Inject a fake client "
            "(verify_against_source takes anthropic_client=) or request the "
            "`llm_calls` fixture if the call is genuinely the point."
        )


@pytest.fixture
def llm_calls(_no_real_llm_calls):
    """Opt in to (still-blocked) LLM calls, and inspect the attempted URLs."""
    return _no_real_llm_calls


@pytest.fixture(scope='session')
def temp_db_dir():
    """Create a temporary directory for test databases."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Create test app via factory with temp data dir."""
    import carrac.services as svc
    from carrac import db_pool as _db_pool
    svc._fallback_container = None  # Force fresh ServiceContainer build
    _db_pool._db_type = None        # Re-read DB_TYPE env

    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    monkeypatch.setenv('DATA_DIR', str(data_dir))

    # Rebind the user_db singleton to THIS test's isolated DB. Without this the
    # singleton keeps writing to whatever users.db it first opened (see
    # rebind_user_db docstring), so the app and direct user_db calls in a test
    # disagree and count/dedup assertions flake under xdist.
    rebind_user_db(data_dir)

    # Lightweight OHLCVDatabase init — skip network-dependent rate limiter
    import database

    def _patched_init(self, **kwargs):
        from unittest.mock import MagicMock
        self.cache_dir = data_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.dynamic_cache_dir = data_dir / 'dynamic'
        self.dynamic_cache_dir.mkdir(parents=True, exist_ok=True)
        self.rate_limiter = None
        self._last_binance_ohlcv = {}
        self._binance_exchange_info = set()
        self._binance_exchange_info_time = None
        self._exchange_info_lock = __import__('threading').Lock()
        # Maps needed by _get_all_symbols_set / is_curated / validate_symbol
        self.crypto_map = {}
        self.crypto_metric_map = {}
        self.binance_map = {}
        self.stock_map = {}
        self.etf_map = {}
        self.index_map = {}
        self.commodity_map = {}
        self.forex_map = {}
        self.ratio_map = {}
        self._asset_info = {}
        # Stub client objects so validate_symbol / search don't crash
        self.binance = MagicMock()
        self.binance.get_exchange_info.return_value = self._binance_exchange_info
        self.yahoo = MagicMock()
        self.yahoo.validate_symbol.return_value = None
        self.yahoo.resolve_symbol = lambda s: s
        self.cache = MagicMock()
        self.cache.load_dynamic.return_value = None

    monkeypatch.setattr(database.OHLCVDatabase, '__init__', _patched_init)

    from carrac import create_app
    from carrac.config import TestConfig
    TestConfig.DATA_DIR = str(data_dir)
    application = create_app(TestConfig)
    application.config['WTF_CSRF_ENABLED'] = False
    # Clear the per-process AI chat concurrency lock between tests so
    # one test's chat request doesn't leak a "in progress" entry into
    # the next test. State is now in carrac.ai.chat_slot (shared between
    # Flask and FastAPI); clear both the shared module and the legacy attr
    # on routes (removed but kept here for safety during migration).
    try:
        import carrac.ai.chat_slot as _chat_slot
        with _chat_slot._active_chats_lock:
            _chat_slot._active_chats.clear()
    except Exception:
        pass

    # Clear the per-user session kill-epoch fallback between tests.
    # It's a process-global dict (Redis is the real store in production); without a
    # reset, a test that deletes/kills user_id N poisons a later test that
    # injects a session for the same id via session_transaction() — injected
    # sessions carry no _created_at (defaults to epoch 0) and so trip the
    # kill-epoch gate.
    try:
        import carrac.auth.routes as _auth_routes
        _auth_routes._KILL_TS_FALLBACK.clear()
    except Exception:
        pass

    # Override inject_cache_buster with a deterministic static version for tests
    # (create_app's default reads file mtime, which is non-deterministic in CI)
    @application.context_processor
    def _inject_test_cache_version():
        return dict(cache_version=lambda _f: '0')

    yield application
    svc._fallback_container = None


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def auth_client(client):
    """Authenticated test client (session login, member tier)."""
    with client.session_transaction() as sess:
        sess['user_id'] = 'test-user'
        sess['email'] = 'test@example.com'
        sess['is_admin'] = False
        sess['tier'] = 'member'
    return client


@pytest.fixture
def admin_client(client):
    """Admin-authenticated test client."""
    with client.session_transaction() as sess:
        sess['user_id'] = 'admin'
        sess['email'] = 'admin@example.com'
        sess['is_admin'] = True
    return client


@pytest.fixture
def bind_mcp_user(monkeypatch):
    """Bind a per-user MCP user id for direct tool-handler calls.

    Tests that invoke MCP tool handlers directly via TOOL_REGISTRY (not via
    the /mcp endpoint) run without Flask's g-binding, so get_mcp_user_id()
    would raise. This fixture monkeypatches the helper in every tools module
    so handlers see a valid user id.

    Default user_id=42. Pass a different id via `bind_mcp_user(uid=99)`.
    """
    class _Binder:
        def __call__(self, uid: int = 42):
            for mod in (
                'carrac.mcp.tools',
                'carrac.mcp.tools.plays',
                'carrac.mcp.tools.alerts',
                'carrac.mcp.tools.journal',
                'carrac.mcp.tools.bridges',
                'carrac.mcp.tools.regime',
                'carrac.mcp.tools.quant',
                'carrac.mcp.tools.backtest',
                'carrac.mcp.tools.backtests',
                # watchlist/recipes import get_mcp_user_id at
                # module level (like bridges/alerts above), so the shared
                # 'carrac.mcp.tools' package-attribute patch above doesn't
                # reach their already-bound local name — needs its own entry.
                'carrac.mcp.tools.watchlist',
                'carrac.mcp.tools.recipes',
            ):
                monkeypatch.setattr(f'{mod}.get_mcp_user_id', lambda u=uid: u, raising=False)
            return uid
    binder = _Binder()
    binder()  # bind default user_id=42 eagerly
    return binder


@pytest.fixture
def mock_api_response():
    """Factory fixture for creating mock API responses."""
    def _create_response(data, status_code=200):
        from unittest.mock import Mock
        response = Mock()
        response.json.return_value = data
        response.status_code = status_code
        response.raise_for_status = Mock()
        if status_code >= 400:
            from requests.exceptions import HTTPError
            response.raise_for_status.side_effect = HTTPError()
        return response
    return _create_response


@pytest.fixture
def sample_price_data():
    """Sample price data for testing."""
    return [
        100, 102, 98, 105, 103, 107, 104, 110, 108, 112,
        106, 115, 111, 118, 114, 120, 116, 122, 119, 125
    ]


@pytest.fixture
def sample_volume_data():
    """Sample volume data for testing."""
    return [
        1000, 1200, 900, 1500, 1300, 1700, 1400, 2000, 1800, 2200,
        1600, 2500, 2100, 2800, 2400, 3000, 2600, 3200, 2900, 3500
    ]


@pytest.fixture
def sample_ohlcv_data():
    """Sample OHLCV data for testing."""
    return {
        'open': [100, 102, 98, 105, 103],
        'high': [105, 107, 103, 110, 108],
        'low': [98, 100, 95, 102, 100],
        'close': [102, 98, 105, 103, 107],
        'volume': [1000, 1200, 900, 1500, 1300]
    }


# ---------------------------------------------------------------------------
# Postgres idle-in-transaction leak detector
# ---------------------------------------------------------------------------
#
# SAFETY NET: This fixture detects connections that are left "idle in
# transaction" after each test — the leak class that exhausts the pool when
# connections are not released after a read.
#
# HOW IT WORKS
# ─────────────
# A probe psycopg2 connection is opened at session start to the test DB.
# Before and after each test the fixture snapshots the count of
# "idle in transaction" connections in pg_stat_activity.  A rise means a
# connection was left open without commit/rollback.
#
# MODES
# ──────
#   WARN (default) — emits a Python warning per leaky test; CI stays green.
#   FAIL mode      — set env var LEAK_FIXTURE_FAIL=1; asserts after > before.
#                    Use this for PR gating once the baseline reaches zero.
#
# CURRENT DB TYPE IN THE LOCAL TEST SUITE
# ─────────────────────────────────────────
# The local test suite forces DB_TYPE=sqlite (set at the top of this file).  Without a
# real Postgres instance the probe connection cannot be established.  In that
# case the fixture degrades to a no-op stub: zero overhead, no failures.
#
# FASTAPI MIGRATION NOTE
# ───────────────────
# The safety net is INCOMPLETE while the suite runs on SQLite.  To make it
# effective during the migration:
#   1. Spin up a throwaway Postgres in CI (docker service / pytest-postgresql).
#   2. Set TEST_DATABASE_URL=postgresql://... in the CI environment.
#   3. Do NOT set DB_TYPE=sqlite so the app code uses the real pool.
# Once that is in place, flip LEAK_FIXTURE_FAIL=1 and fix every pre-existing
# leak the fixture surfaces before merging any new endpoint.
#
# PROBE CONNECTION
# ─────────────────
# The probe reads TEST_DATABASE_URL first, then DATABASE_URL.  It never uses
# the app's own pool — a separate psycopg2 connection is opened so the probe
# itself can never hold a slot in the pool under test.
# ---------------------------------------------------------------------------

_PROBE_DSN_ENV_VARS = ('TEST_DATABASE_URL', 'DATABASE_URL')


def _resolve_probe_dsn() -> "str | None":
    """Return the first non-empty Postgres DSN from the probe env vars, or None."""
    for var in _PROBE_DSN_ENV_VARS:
        val = os.environ.get(var, '').strip()
        if val and val.startswith('postgresql'):
            return val
    return None


@pytest.fixture(scope='session')
def _leak_probe_conn():
    """
    Session-scoped probe connection to the test Postgres instance.

    Yields a live psycopg2 connection or None if no Postgres is reachable.
    No DSN configured is the normal case (SQLite runs) and is silent: the
    detector simply does nothing. A DSN that is set but unreachable is a
    misconfiguration and warns.
    """
    dsn = _resolve_probe_dsn()
    if dsn is None:
        yield None
        return

    conn = None
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.autocommit = True  # probe must never hold its own transaction
        yield conn
    except Exception as exc:  # noqa: BLE001
        warnings.warn(
            f"[LEAK-FIXTURE] Could not connect to Postgres probe DSN ({exc}). "
            "The idle-in-transaction leak detector is running in NO-OP mode.",
            stacklevel=1,
        )
        yield None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _count_idle_in_tx(conn) -> int:
    """Return count of 'idle in transaction' connections for current DB."""
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE state = 'idle in transaction' "
            "AND datname = current_database()"
        )
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


@pytest.fixture(autouse=True)
def _leak_detector(request, _leak_probe_conn):
    """
    Autouse per-test fixture: detect idle-in-transaction connection leaks.

    Before the test: snapshot idle-in-tx count via the probe connection.
    After  the test: re-check and compare.

    A positive delta triggers a warning (WARN mode, default) or an assertion
    failure (FAIL mode — set LEAK_FIXTURE_FAIL=1).

    When the probe connection is None (SQLite-only CI) this fixture is a
    zero-overhead no-op.
    """
    conn = _leak_probe_conn
    if conn is None:
        yield
        return

    before = _count_idle_in_tx(conn)
    yield
    after = _count_idle_in_tx(conn)

    delta = after - before
    if delta > 0:
        msg = (
            f"[LEAK-FIXTURE] test={request.node.nodeid} "
            f"grew idle-in-tx by {delta} "
            f"(before={before} after={after})"
        )
        if os.environ.get('LEAK_FIXTURE_FAIL', '').strip() == '1':
            assert after <= before, msg
        else:
            warnings.warn(msg, stacklevel=2)


# Configure pytest
def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )


@pytest.fixture
def admin_caller():
    """Run the test as the operator.

    Deribit-derived data is admin-only, and the gate fails closed — with no
    caller set, ``get_mcp_caller_tier()`` returns 'free' and the columns are
    stripped. Tests that exercise the FULL derivatives path (options metrics,
    regime classification off them, per-asset DERIV_VARS) must therefore
    declare who they are, exactly as a real admin request does.

    Tests asserting the PUBLIC shape should NOT use this fixture — the
    default no-caller state is the public one.
    """
    from carrac.mcp.runtime import reset_mcp_caller, set_mcp_caller

    set_mcp_caller(1, 'admin')
    try:
        yield
    finally:
        reset_mcp_caller()
