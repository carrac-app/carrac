"""An upstream outage must DEGRADE /api/market-sentiment, not hang it.

When FRED times out, /api/market-sentiment returns within its bounded budget:

  A. `FredClient` is shared across calls, so its CircuitBreaker and its 1-4h
     per-series cache persist and `is_open()` short-circuits a failing upstream.
  B. `_compute_sentiment()` bounds the request: `future.result(timeout=30)`
     gives up on a hung FRED task without the executor shutdown waiting for it.
  C. The stale-while-revalidate branch serves the stale answer while a refresh
     is in flight instead of computing synchronously on every request.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

import data_sources.fred as fred_mod
from data_sources.fred import FredClient


@pytest.fixture(autouse=True)
def _reset_sentiment_response_cache():
    """`_sentiment_response_cache` is a module-level dict shared by
    every test that runs in this PROCESS — an xdist worker runs many test
    files serially in one interpreter. tests/test_app.py and tests/test_auth.py
    both hit the real `/api/market-sentiment` route to exercise auth patterns,
    which on a 200 populates this cache for `_SENTIMENT_RESPONSE_TTL` (120s).
    If one of those happens to run first in the same worker,
    `TestStaleServedWhileRefreshing` below would see THAT leftover payload at
    step 2 of `api_market_sentiment()` — before ever reaching the
    stale/refresh branch this class exists to test — and assert on a
    `sentiment.marker` key that was never written. That is the
    `KeyError: 'marker'` flake seen once under `-n 4`: test
    order (hence which cache state exists) is not guaranteed across files
    collected into the same worker.
    """
    from carrac.sentiment import routes
    routes._sentiment_response_cache.pop('data', None)
    yield
    routes._sentiment_response_cache.pop('data', None)


@pytest.fixture
def fresh_shared_fred(monkeypatch):
    """Give each test its own shared client so breaker state cannot leak."""
    monkeypatch.setenv('FRED_API_KEY', 'test-key')  # else _fetch_series short-circuits
    monkeypatch.setattr(fred_mod, '_shared_client', None, raising=False)
    monkeypatch.setattr(FredClient, 'BACKOFF_BASE', 0.001)  # keep the test quick
    client = fred_mod.get_fred_client()
    yield client
    monkeypatch.setattr(fred_mod, '_shared_client', None, raising=False)


class TestSharedFredClient:
    """Defect A — the breaker and the cache must outlive a single request."""

    def test_get_fred_client_is_process_wide(self, fresh_shared_fred):
        assert fred_mod.get_fred_client() is fred_mod.get_fred_client()

    def test_breaker_opens_and_then_spares_later_callers(self, fresh_shared_fred,
                                                         notifications):
        """Show it failing: with a per-call client this count never stops rising.

        `notifications` is requested deliberately: the ntfy push on
        the CLOSED→OPEN transition is the human-reaching alarm for this failure,
        so it is asserted rather than stubbed away.
        """
        client = fresh_shared_fred
        attempts = []

        def dead_upstream(*a, **kw):
            attempts.append(1)
            raise requests.exceptions.ConnectTimeout('simulated FRED outage')

        client._session.request = dead_upstream

        for _ in range(6):
            client.get_fed_funds()

        # BREAKER_THRESHOLD=3 terminal failures × MAX_RETRIES=3 attempts each.
        # Everything after the breaker opens must short-circuit with no HTTP.
        assert len(attempts) == FredClient.MAX_RETRIES * FredClient.BREAKER_THRESHOLD
        assert client.breaker.is_open()

        # The operator finds out: one ntfy on the transition, and exactly one —
        # a per-instance breaker also defeated NOTIFY_COOLDOWN_SECS, because the
        # instance holding the cooldown clock died with the request.
        opens = [n for n in notifications
                 if 'Circuit breaker OPEN: fred' in str(n.get('kwargs', {}).get('body', ''))]
        assert len(opens) == 1, f'expected exactly one OPEN alarm, got {notifications}'

    def test_series_cache_survives_across_callers(self, fresh_shared_fred):
        """The documented TTL_DAILY=3600 cache never had a cross-request hit."""
        client = fresh_shared_fred
        calls = []

        def ok(*a, **kw):
            calls.append(1)
            resp = MagicMock(spec=requests.Response)
            resp.status_code = 200
            resp.ok = True
            resp.json.return_value = {'observations': [
                {'date': '2026-08-25', 'value': '4.33'},
                {'date': '2026-08-24', 'value': '4.33'},
            ]}
            return resp

        client._session.request = ok

        fred_mod.get_fred_client().get_fed_funds()
        fred_mod.get_fred_client().get_fed_funds()
        fred_mod.get_fred_client().get_fed_funds()

        assert len(calls) == 1, 'second and third callers must hit the TTL cache'

    def test_no_production_call_site_builds_its_own_client(self):
        """Sibling sweep, enforced: every prod site must go through the accessor."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        skip_dirs = {'tests', 'scripts', 'node_modules', '__pycache__'}
        hits = []
        for path in root.rglob('*.py'):
            rel = path.relative_to(root)
            if rel.parts[0] in skip_dirs or any(p.startswith('.') for p in rel.parts):
                continue
            if rel.as_posix() == 'data_sources/fred.py':
                continue
            for n, line in enumerate(path.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
                if 'FredClient()' in line:
                    hits.append(f'{rel}:{n}: {line.strip()}')
        assert hits == [], 'per-call FredClient() construction still present:\n' + '\n'.join(hits)


class TestComputeIsBounded:
    """Defect B — a hung upstream must not extend the request past the budget."""

    def test_compute_returns_within_budget_when_a_source_hangs(self, monkeypatch):
        from carrac.sentiment import routes

        hanging = threading.Event()

        class HangingFred:
            def get_all(self):
                hanging.wait(30)     # far longer than the budget
                return {'m2': None}

        monkeypatch.setattr(fred_mod, 'get_fred_client', lambda: HangingFred())
        monkeypatch.setattr(routes, '_get_db', lambda: MagicMock())
        monkeypatch.setattr(routes, '_fetch_fear_greed',
                            lambda: {'value': 50, 'classification': 'Neutral'})
        monkeypatch.setattr('carrac.sentiment.regime.get_or_compute_regime',
                            lambda: {'regime': 'risk-on', 'score': 1})

        started = time.monotonic()
        try:
            result = routes._compute_sentiment(budget_s=2.0)
            elapsed = time.monotonic() - started
        finally:
            hanging.set()

        assert elapsed < 6.0, f'compute took {elapsed:.1f}s against a 2s budget'
        assert result['sentiment']['fred'] == {}, 'the hung source must fall back'

    def test_budget_exhaustion_reaches_a_human(self, monkeypatch):
        """A degraded payload must beacon to the error tracker, not just a logfile.

        The 79.30s request that prompted this fix was fully logged and went
        unread for a day. `_capture` is the seam; assert it actually fires.
        """
        from carrac.sentiment import routes

        hanging = threading.Event()

        class HangingFred:
            def get_all(self):
                hanging.wait(30)
                return {}

        captured = []
        monkeypatch.setattr(routes, '_capture', captured.append)
        monkeypatch.setattr(fred_mod, 'get_fred_client', lambda: HangingFred())
        monkeypatch.setattr(routes, '_get_db', lambda: MagicMock())
        monkeypatch.setattr(routes, '_fetch_fear_greed',
                            lambda: {'value': 50, 'classification': 'Neutral'})
        monkeypatch.setattr('carrac.sentiment.regime.get_or_compute_regime',
                            lambda: {'regime': 'risk-on', 'score': 1})

        try:
            routes._compute_sentiment(budget_s=1.0)
        finally:
            hanging.set()

        assert captured, 'budget exhaustion must reach Bugsink'
        assert 'upstream-degraded' in captured[0] and "'fred'" in captured[0]


class TestFredDependentTasksHaveASubBudget:
    """'fred' and 'regime' both bottleneck on the same FRED
    upstream and the same 2-slot `_FRED_REQUEST_SEMAPHORE`
    (data_sources/fred.py) — `regime.py::gather_regime_inputs()` makes 3 more
    sequential FRED calls on top of `fred`'s own 5-way `get_all()` fan-out.

    Reordering the `tasks` dict in `_compute_sentiment` would NOT fix this:
    every future is submitted concurrently before the result loop runs, so
    each task's actual completion time is independent of which key is
    checked first — `future.result(timeout=remaining)` just waits for the
    SAME wall-clock deadline no matter the iteration order. What actually
    keeps one hung FRED-bound task from eating the entire request budget
    (and starving whichever of {fred, regime} is checked later, or the
    response itself) is capping how long the loop will wait for EACH of the
    two individually — see `_SENTIMENT_FRED_SUBBUDGET_FRACTION` in routes.py.

    The shipped default for that fraction is 1.0 (no cap): capping it by default would convert FRED tasks that complete
    WITHIN the full budget into forced degradations, which increases blank
    widgets rather than reducing them. The two tests below dial the fraction
    down explicitly to prove the capping mechanism itself still works when an
    operator needs it; `test_default_fraction_gives_fred_the_full_budget`
    proves the shipped default does NOT cap.
    """

    def test_fred_is_bounded_to_a_fraction_of_the_overall_budget(self, monkeypatch):
        from carrac.sentiment import routes

        monkeypatch.setattr(routes, '_SENTIMENT_FRED_SUBBUDGET_FRACTION', 0.5)

        hanging = threading.Event()

        class HangingFred:
            def get_all(self):
                hanging.wait(30)
                return {}

        monkeypatch.setattr(fred_mod, 'get_fred_client', lambda: HangingFred())
        monkeypatch.setattr(routes, '_get_db', lambda: MagicMock())
        monkeypatch.setattr(routes, '_fetch_fear_greed',
                            lambda: {'value': 50, 'classification': 'Neutral'})
        monkeypatch.setattr('carrac.sentiment.regime.get_or_compute_regime',
                            lambda: {'regime': 'risk-on', 'score': 1})

        started = time.monotonic()
        try:
            result = routes._compute_sentiment(budget_s=10.0)
            elapsed = time.monotonic() - started
        finally:
            hanging.set()

        # With the fraction forced to 0.5: 'fred' is checked with `remaining`
        # == ~the full 10s budget (the tasks ahead of it in the dict are fast,
        # non-hanging calls) UNLESS the cap applies, in which case it is
        # capped at 10 * 0.5 == ~5s.
        assert elapsed < 7.0, f"'fred' consumed more than its sub-budget: {elapsed:.1f}s"
        assert result['sentiment']['fred'] == {}

    def test_regime_is_bounded_to_a_fraction_of_the_overall_budget(self, monkeypatch):
        from carrac.sentiment import routes

        monkeypatch.setattr(routes, '_SENTIMENT_FRED_SUBBUDGET_FRACTION', 0.5)

        hanging = threading.Event()

        def hanging_regime():
            hanging.wait(30)
            return {'regime': 'risk-on'}

        monkeypatch.setattr(fred_mod, 'get_fred_client',
                            lambda: type('FastFred', (), {'get_all': lambda self: {}})())
        monkeypatch.setattr(routes, '_get_db', lambda: MagicMock())
        monkeypatch.setattr(routes, '_fetch_fear_greed',
                            lambda: {'value': 50, 'classification': 'Neutral'})
        monkeypatch.setattr('carrac.sentiment.regime.get_or_compute_regime', hanging_regime)

        started = time.monotonic()
        try:
            result = routes._compute_sentiment(budget_s=10.0)
            elapsed = time.monotonic() - started
        finally:
            hanging.set()

        assert elapsed < 7.0, f"'regime' consumed more than its sub-budget: {elapsed:.1f}s"
        assert result['sentiment']['regime'] == {
            'regime': 'N/A', 'score': 0, 'color': '#8b949e', 'components': [],
        }

    def test_default_fraction_gives_fred_the_full_budget(self, monkeypatch):
        """The shipped default must NOT cap — a FRED
        task that would complete within the full budget on its own must be
        allowed to, not forced into an early degradation. Deliberately does
        NOT monkeypatch the fraction, so this exercises the real shipped
        constant.
        """
        from carrac.sentiment import routes

        assert routes._SENTIMENT_FRED_SUBBUDGET_FRACTION == 1.0, (
            'shipped default changed — this test and the comment above '
            '_SENTIMENT_FRED_SUBBUDGET_FRACTION need to be revisited together'
        )

        hanging = threading.Event()

        class HangingFred:
            def get_all(self):
                hanging.wait(30)
                return {}

        monkeypatch.setattr(fred_mod, 'get_fred_client', lambda: HangingFred())
        monkeypatch.setattr(routes, '_get_db', lambda: MagicMock())
        monkeypatch.setattr(routes, '_fetch_fear_greed',
                            lambda: {'value': 50, 'classification': 'Neutral'})
        monkeypatch.setattr('carrac.sentiment.regime.get_or_compute_regime',
                            lambda: {'regime': 'risk-on', 'score': 1})

        started = time.monotonic()
        try:
            result = routes._compute_sentiment(budget_s=2.0)
            elapsed = time.monotonic() - started
        finally:
            hanging.set()

        # No cap at the default: 'fred' should run right up to the full 2s
        # budget, not bail early at a fraction of it.
        assert elapsed >= 1.9, (
            f"default fraction capped 'fred' short of the full budget: {elapsed:.2f}s"
        )
        assert result['sentiment']['fred'] == {}


class TestStaleServedWhileRefreshing:
    """Defect C — a refresh in flight is the strongest reason to serve stale."""

    def test_stale_is_returned_even_when_a_refresh_is_running(self, auth_client):
        stale_payload = {'status': 'ok', 'sentiment': {'marker': 'stale-hit'}}

        with patch('carrac.caches.get_cached_sentiment', return_value=None), \
             patch('carrac.caches.get_stale_sentiment', return_value=stale_payload), \
             patch('carrac.caches.is_sentiment_refreshing', return_value=True), \
             patch('carrac.sentiment.routes._compute_sentiment') as compute:
            resp = auth_client.get('/api/market-sentiment')

        assert resp.status_code == 200
        assert resp.get_json()['sentiment']['marker'] == 'stale-hit'
        compute.assert_not_called()

    def test_no_second_refresh_thread_is_started_while_one_runs(self, auth_client):
        stale_payload = {'status': 'ok', 'sentiment': {'marker': 'stale-hit'}}

        with patch('carrac.caches.get_cached_sentiment', return_value=None), \
             patch('carrac.caches.get_stale_sentiment', return_value=stale_payload), \
             patch('carrac.caches.is_sentiment_refreshing', return_value=True), \
             patch('carrac.caches.set_sentiment_refreshing') as set_flag, \
             patch('carrac.sentiment.routes.threading') as routes_threading:
            # Patch the `threading` NAME bound in the routes module, not the
            # global threading.Thread class: under `pytest -n 4` the latter
            # also swallowed the lazy ThreadPoolExecutor's own worker threads
            # (concurrent.futures resolves threading.Thread at call time) and
            # the assertion below flaked on whichever test warmed it first.
            auth_client.get('/api/market-sentiment')

        routes_threading.Thread.assert_not_called()
        set_flag.assert_not_called()
