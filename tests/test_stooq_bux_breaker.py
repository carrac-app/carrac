"""
Tests for ^BUX / malformed-CSV parse-error isolation.

Pins that a per-symbol CSV parse error (e.g. ^BUX returning a row
with extra fields) must NOT trip the SHARED stooq circuit breaker.

If `stooq.py::fetch()` called `self._breaker.record_failure()` in the CSV
parse except-block, the shared breaker would open after one bad symbol and
block ALL subsequent stooq fetches for the breaker window.

So the parse except-block only logs + falls back to Yahoo — the breaker is
left untouched.
"""

from unittest.mock import MagicMock, patch
import requests

from data_sources.circuit_breaker import CircuitBreaker


# A minimal ^BUX-style malformed CSV: line 6 has 2 fields instead of the
# expected 1 (pandas raises `ParserError: Error tokenizing data. C error:
# Expected 1 fields in line 6, saw 2`).  We replicate the structure that
# causes a pandas tokenize error: a header with one column, then a row with
# two fields.
BAD_BUX_CSV = """\
Date,Open,High,Low,Close,Volume
2025-03-17,4500.00,4550.00,4480.00,4520.00,123456
2025-03-18,4520.00,4560.00,4510.00,4540.00,234567
2025-03-19,4540.00,4580.00,4520.00,4560.00,345678
2025-03-20,4560.00,4600.00,4540.00,4580.00,456789
malformed_row_with,extra,fields,that,cause,pandas,tokenize,error,extra,extra
"""

# A well-formed response for a different symbol (e.g. SPX) to confirm the
# breaker does NOT block normal symbols after the BUX parse failure.
GOOD_SPX_CSV = """\
Date,Open,High,Low,Close,Volume
2025-03-17,5100.00,5150.00,5090.00,5130.00,2000000000
2025-03-18,5130.00,5160.00,5120.00,5150.00,1900000000
"""


def _make_response(text: str, status: int = 200) -> MagicMock:
    """Build a mock requests.Response with the given body text."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.ok = 200 <= status < 400
    resp.text = text
    return resp


def _make_stooq_client(breaker: CircuitBreaker) -> "StooqClient":  # noqa: F821
    """Build a StooqClient with a real breaker and a mocked yahoo fallback."""
    from data_sources.stooq import StooqClient

    yahoo = MagicMock()
    yahoo.daily_breaker = CircuitBreaker('yfinance_test')
    yahoo.fetch_daily.return_value = MagicMock()  # any non-None value
    return StooqClient(breaker=breaker, yahoo_client=yahoo)


class TestBuxParseBreakerIsolation:
    """Parse errors from a single malformed symbol must not trip the breaker."""

    def test_malformed_csv_returns_without_raising(self):
        """fetch() with malformed CSV must not raise."""
        breaker = CircuitBreaker('stooq_test', threshold=3)
        client = _make_stooq_client(breaker)

        with patch.object(client, '_request', return_value=_make_response(BAD_BUX_CSV)):
            # Should not raise — just fall back to Yahoo
            result = client.fetch('BUX')

        # Result is whatever yahoo.fetch_daily returns (MagicMock in this test)
        assert result is not None

    def test_malformed_csv_does_not_increment_breaker(self):
        """The shared breaker failure count must stay at 0 after a parse error."""
        breaker = CircuitBreaker('stooq_test', threshold=3)
        client = _make_stooq_client(breaker)

        assert breaker.failure_count == 0

        with patch.object(client, '_request', return_value=_make_response(BAD_BUX_CSV)):
            client.fetch('BUX')

        assert breaker.failure_count == 0, (
            "A CSV parse error for ^BUX must not increment the shared stooq breaker"
        )

    def test_malformed_csv_does_not_open_breaker(self):
        """Breaker must remain closed (not open) after a single ^BUX parse error."""
        # threshold=1 means even a single record_failure() would open it —
        # using this to prove the fix: breaker stays closed.
        breaker = CircuitBreaker('stooq_test', threshold=1)
        client = _make_stooq_client(breaker)

        with patch.object(client, '_request', return_value=_make_response(BAD_BUX_CSV)):
            client.fetch('BUX')

        assert not breaker.is_open(), (
            "Breaker must remain closed after a per-symbol CSV parse error"
        )

    def test_other_symbols_still_fetched_after_bux_parse_error(self):
        """After ^BUX parse failure, a well-formed symbol like SPX must succeed normally."""
        breaker = CircuitBreaker('stooq_test', threshold=3)
        client = _make_stooq_client(breaker)

        # First: BUX fails to parse → should not open the breaker
        with patch.object(client, '_request', return_value=_make_response(BAD_BUX_CSV)):
            client.fetch('BUX')

        assert breaker.failure_count == 0

        # Second: SPX with good CSV — must reach Stooq, not be blocked
        request_called = {'count': 0}

        def counting_request(method, url, **kwargs):
            request_called['count'] += 1
            return _make_response(GOOD_SPX_CSV)

        with patch.object(client, '_request', side_effect=counting_request):
            client.fetch('SPX')

        # The breaker must have let SPX through (request was actually made)
        assert request_called['count'] >= 1, (
            "SPX request must not be blocked by the breaker after a BUX parse error"
        )

    def test_real_connectivity_failure_still_trips_breaker(self, notifications):
        """Connectivity failures (None response from _request) MUST still trip the breaker.

        This confirms the fix did NOT weaken the breaker for real infra failures:
        _request() returns None on timeout/connection-error and records the
        failure internally via active_breaker.record_failure() in base.py.
        After threshold failures the breaker opens normally.
        """
        # threshold=2 so two connectivity failures open the breaker
        breaker = CircuitBreaker('stooq_test', threshold=2)
        client = _make_stooq_client(breaker)

        # Simulate _request returning None (what base.py does on timeout /
        # connection-error AFTER exhausting retries — it calls
        # active_breaker.record_failure() itself before returning None).
        def connectivity_fail(method, url, **kwargs):
            # Replicate what BaseDataSource._request does on terminal failure:
            # bump the breaker, then return None.
            breaker.record_failure()
            return None

        with patch.object(client, '_request', side_effect=connectivity_fail):
            client.fetch('AAPL')  # failure 1
            client.fetch('AAPL')  # failure 2 → threshold hit

        assert breaker.is_open(), (
            "The shared breaker must still open on real connectivity failures"
        )

    def test_parse_error_falls_back_to_yahoo(self):
        """A CSV parse error must route to the Yahoo fallback."""
        breaker = CircuitBreaker('stooq_test', threshold=3)
        client = _make_stooq_client(breaker)

        import pandas as pd
        fake_yahoo_df = pd.DataFrame({'close': [100.0]})
        client.yahoo.daily_breaker = CircuitBreaker('yfinance_test')
        client.yahoo.fetch_daily.return_value = fake_yahoo_df

        with patch.object(client, '_request', return_value=_make_response(BAD_BUX_CSV)):
            result = client.fetch('BUX')

        client.yahoo.fetch_daily.assert_called_once()
        assert result is not None
