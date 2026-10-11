"""
Unit tests for data_sources.base.BaseDataSource.

Covers:
  - Retry on 5xx / 429 with backoff.
  - No retry on 4xx (default) and retry_4xx=True opt-in.
  - Circuit breaker opens after N failures, short-circuits subsequent calls.
  - Circuit breaker half-open probe after HALF_OPEN_AFTER seconds.
  - Rate-limit hook short-circuits without HTTP.
  - Successful call resets the breaker and calls _record_call().
  - Sentry breadcrumbs emitted (smoke only — we just ensure no exception).
"""

from unittest.mock import MagicMock

import requests

from data_sources.base import BaseDataSource


class FakeClient(BaseDataSource):
    NAME = 'fake'
    DEFAULT_TIMEOUT = 1.0
    MAX_RETRIES = 3
    BACKOFF_BASE = 0.001
    BREAKER_THRESHOLD = 2
    HALF_OPEN_AFTER = 0.05

    def __init__(self, *, rate_limit_ok=True, session=None, breaker=None):
        super().__init__(session=session, breaker=breaker)
        self._rate_limit_ok = rate_limit_ok
        self.record_count = 0

    def _check_rate_limit(self, provider=None):
        if self._rate_limit_ok:
            return True, ''
        return False, 'test rate limit'

    def _record_call(self, count=1, provider=None):
        self.record_count += count


def _resp(status=200):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.ok = 200 <= status < 400
    return r


def test_success_resets_breaker_and_records_call():
    session = MagicMock()
    session.request.return_value = _resp(200)
    client = FakeClient(session=session)
    # Pre-seed a failure
    client.breaker.record_failure()
    assert client.breaker.failure_count == 1

    resp = client._request('GET', 'http://example')

    assert resp is not None
    assert resp.status_code == 200
    assert client.breaker.failure_count == 0
    assert client.record_count == 1
    assert session.request.call_count == 1


def test_retry_on_500_succeeds_after_attempts():
    session = MagicMock()
    session.request.side_effect = [_resp(500), _resp(502), _resp(200)]
    client = FakeClient(session=session)

    resp = client._request('GET', 'http://example')

    assert resp is not None
    assert resp.status_code == 200
    assert session.request.call_count == 3


def test_retry_on_429_counts_toward_max():
    session = MagicMock()
    session.request.side_effect = [_resp(429), _resp(429), _resp(429)]
    client = FakeClient(session=session)

    resp = client._request('GET', 'http://example')

    assert resp is None
    # Breaker should bump after exhausting retries
    assert client.breaker.failure_count == 1
    assert session.request.call_count == 3


def test_no_retry_on_404_by_default():
    session = MagicMock()
    session.request.return_value = _resp(404)
    client = FakeClient(session=session)

    resp = client._request('GET', 'http://example')

    # 4xx returns the Response (caller handles), does NOT bump breaker
    assert resp is not None
    assert resp.status_code == 404
    assert client.breaker.failure_count == 0
    assert session.request.call_count == 1


def test_retry_4xx_opt_in():
    session = MagicMock()
    session.request.side_effect = [_resp(429), _resp(200)]
    client = FakeClient(session=session)

    resp = client._request('GET', 'http://example', retry_4xx=True)

    assert resp is not None
    assert resp.status_code == 200
    assert session.request.call_count == 2


def test_breaker_opens_after_threshold_and_short_circuits(notifications):
    # Opening the breaker fires a real "⚡ Carrac data source alert" push
    # (per-instance cooldown, so every fresh breaker sends once).
    session = MagicMock()
    session.request.side_effect = [
        requests.Timeout(),     # call 1, all 3 attempts timeout
        requests.Timeout(),
        requests.Timeout(),
        requests.Timeout(),     # call 2, all 3 attempts timeout
        requests.Timeout(),
        requests.Timeout(),
    ]
    client = FakeClient(session=session)

    # First call → breaker bump 1
    assert client._request('GET', 'http://example') is None
    # Second call → breaker bump 2 → OPEN
    assert client._request('GET', 'http://example') is None
    assert client.breaker.is_open()

    # Third call short-circuits — no HTTP issued
    session.request.reset_mock()
    result = client._request('GET', 'http://example')
    assert result is None
    assert session.request.call_count == 0


def test_breaker_half_open_probe_allows_one_call(monkeypatch, notifications):
    # Opening the breaker fires a real "⚡ Carrac data source alert" push.
    # The probe mechanics are the subject here, so keep the default notify
    # path exercised and let the notification-send recorder capture the send.
    session = MagicMock()
    session.request.side_effect = [
        requests.Timeout(), requests.Timeout(), requests.Timeout(),
        requests.Timeout(), requests.Timeout(), requests.Timeout(),
        _resp(200),  # probe succeeds
    ]
    client = FakeClient(session=session)

    # Open the breaker
    client._request('GET', 'http://example')
    client._request('GET', 'http://example')
    assert client.breaker.is_open()

    # Advance past HALF_OPEN_AFTER window
    import time
    time.sleep(0.06)

    # Probe allowed → success resets breaker
    resp = client._request('GET', 'http://example')
    assert resp is not None
    assert resp.status_code == 200
    assert client.breaker.failure_count == 0


def test_rate_limit_hook_skips_http():
    session = MagicMock()
    client = FakeClient(session=session, rate_limit_ok=False)

    resp = client._request('GET', 'http://example')

    assert resp is None
    assert session.request.call_count == 0
    # Rate limit is not a server failure — breaker should not bump
    assert client.breaker.failure_count == 0


def test_timeout_triggers_retry_then_breaker():
    session = MagicMock()
    session.request.side_effect = [
        requests.ConnectionError('conn fail'),
        requests.Timeout('timeout'),
        requests.Timeout('timeout'),
    ]
    client = FakeClient(session=session)

    resp = client._request('GET', 'http://example')

    assert resp is None
    assert session.request.call_count == 3
    assert client.breaker.failure_count == 1


def test_sentry_breadcrumb_does_not_break_calls():
    """Breadcrumb code path shouldn't raise even without Sentry configured."""
    session = MagicMock()
    session.request.return_value = _resp(200)
    client = FakeClient(session=session)

    resp = client._request('GET', 'http://example')
    assert resp is not None


def test_non_retryable_requests_exception_bumps_breaker_once():
    session = MagicMock()
    session.request.side_effect = requests.exceptions.InvalidURL('bad url')
    client = FakeClient(session=session)

    resp = client._request('GET', 'http://example')

    # InvalidURL is not transient — no retry, breaker bumps once.
    assert resp is None
    assert session.request.call_count == 1
    assert client.breaker.failure_count == 1


def test_custom_max_retries_override():
    session = MagicMock()
    session.request.return_value = _resp(500)
    client = FakeClient(session=session)

    resp = client._request('GET', 'http://example', max_retries=1)

    # With max_retries=1 we expect exactly one attempt, no retry
    assert resp is None
    assert session.request.call_count == 1
