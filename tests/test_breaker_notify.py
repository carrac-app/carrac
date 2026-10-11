"""
Tests for circuit breaker CLOSED→OPEN operator notifications.

Verifies:
  (a) Transition to OPEN triggers exactly one notify call.
  (b) Repeated failures while already OPEN don't re-notify.
  (c) Cooldown suppresses a second OPEN within the window.
  (d) A notify that raises does NOT propagate — breaker remains functional.

Also covers trip() for completeness.
"""

from unittest.mock import MagicMock, patch

import pytest

from data_sources.circuit_breaker import CircuitBreaker, NOTIFY_COOLDOWN_SECS


_NOTIFY_PATH = "data_sources.circuit_breaker._notify_open"


# ---------------------------------------------------------------------------
# (a) Transition to OPEN triggers exactly one notify
# ---------------------------------------------------------------------------

class TestOpenTransitionNotify:
    def test_record_failure_reaching_threshold_notifies_once(self):
        breaker = CircuitBreaker("test_src", threshold=3)
        with patch(_NOTIFY_PATH) as mock_notify:
            breaker.record_failure()  # 1 — below threshold
            breaker.record_failure()  # 2 — below threshold
            assert mock_notify.call_count == 0
            breaker.record_failure()  # 3 — threshold reached → OPEN
            assert mock_notify.call_count == 1
            mock_notify.assert_called_once_with("test_src")

    def test_trip_notifies_once(self):
        breaker = CircuitBreaker("trip_src", threshold=3)
        with patch(_NOTIFY_PATH) as mock_notify:
            breaker.trip()
            assert mock_notify.call_count == 1
            mock_notify.assert_called_once_with("trip_src")


# ---------------------------------------------------------------------------
# (b) Repeated failures while already OPEN do not re-notify
# ---------------------------------------------------------------------------

class TestNoRenotifyWhileOpen:
    def test_failures_past_threshold_do_not_renotify(self):
        breaker = CircuitBreaker("flood_src", threshold=2)
        with patch(_NOTIFY_PATH) as mock_notify:
            breaker.record_failure()  # 1
            breaker.record_failure()  # 2 → OPEN, one notify
            # Continued failures while already open
            breaker.record_failure()  # 3 — already open
            breaker.record_failure()  # 4 — already open
            breaker.record_failure()  # 5 — already open
            assert mock_notify.call_count == 1, (
                "Must not re-notify for failures past the threshold"
            )

    def test_trip_twice_within_cooldown_notifies_once(self):
        breaker = CircuitBreaker("trip2_src", threshold=2)
        with patch(_NOTIFY_PATH) as mock_notify:
            breaker.trip()  # first trip → notify
            breaker.trip()  # second trip within cooldown → no notify
            assert mock_notify.call_count == 1


# ---------------------------------------------------------------------------
# (c) Cooldown suppresses a second OPEN within the window
# ---------------------------------------------------------------------------

class TestCooldownSuppression:
    def test_second_open_within_cooldown_suppressed(self):
        """Reset then re-open within cooldown: second OPEN must not re-notify."""
        breaker = CircuitBreaker("cd_src", threshold=2)
        with patch(_NOTIFY_PATH) as mock_notify:
            # Open → notify
            breaker.record_failure()
            breaker.record_failure()
            assert mock_notify.call_count == 1

            # Reset (simulates a half-open probe succeeding)
            breaker.reset()

            # Open again immediately (within cooldown)
            breaker.record_failure()
            breaker.record_failure()
            assert mock_notify.call_count == 1, (
                "Re-opening within cooldown window must not fire a second notify"
            )

    def test_second_open_after_cooldown_fires_again(self):
        """After cooldown expires, re-opening must notify again."""
        breaker = CircuitBreaker("cd2_src", threshold=2)
        with patch(_NOTIFY_PATH) as mock_notify:
            # Open → notify
            breaker.record_failure()
            breaker.record_failure()
            assert mock_notify.call_count == 1

            # Fast-forward the cooldown by backdating _last_notified_at
            with breaker._lock:
                breaker._last_notified_at -= NOTIFY_COOLDOWN_SECS + 1

            breaker.reset()

            # Re-open after cooldown expired → second notify
            breaker.record_failure()
            breaker.record_failure()
            assert mock_notify.call_count == 2, (
                "Re-opening after cooldown must fire another notify"
            )


# ---------------------------------------------------------------------------
# (d) Notify raising does NOT propagate — breaker remains functional
# ---------------------------------------------------------------------------

class TestNotifyRaisingDoesNotPropagate:
    def test_notify_exception_does_not_propagate_from_record_failure(self):
        breaker = CircuitBreaker("err_src", threshold=1)
        with patch(_NOTIFY_PATH, side_effect=RuntimeError("network down")):
            # Must not raise
            breaker.record_failure()
        assert breaker.is_open(), "Breaker must be open despite notify failure"

    def test_notify_exception_does_not_propagate_from_trip(self):
        breaker = CircuitBreaker("err_trip", threshold=3)
        with patch(_NOTIFY_PATH, side_effect=OSError("timeout")):
            breaker.trip()  # Must not raise
        assert breaker.is_open(), "Breaker must be open despite notify failure"

    def test_breaker_functional_after_notify_failure(self):
        """After a notify failure, the breaker still correctly short-circuits calls."""
        breaker = CircuitBreaker("post_err_src", threshold=2)
        with patch(_NOTIFY_PATH, side_effect=RuntimeError("oops")):
            breaker.record_failure()
            breaker.record_failure()  # threshold → open, notify fails silently

        assert breaker.is_open(), "Breaker must remain open"
        # Reset still works
        breaker.reset()
        assert not breaker.is_open()


# ---------------------------------------------------------------------------
# (e) ntfy header encoding — the ⚡ title crash
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("real_notification_senders")
class TestNtfyHeaderEncoding:
    """The breaker's first real OPEN alert died with
    "'latin-1' codec can't encode character '\\u26a1'" — requests headers are
    latin-1 only. send_ntfy must RFC-2047-encode non-latin-1 header values
    (ntfy decodes them server-side) and pass ASCII through untouched.

    These assert on what send_ntfy BUILDS, so they need the genuine function
    rather than the notification-send recorder — hence real_notification_senders. Each
    mocks requests.post itself, so nothing leaves the process.
    """

    def _send(self, **kwargs):
        from carrac.notifications.ntfy import send_ntfy
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            ok = send_ntfy("body ⚡ works", topic="t-test", **kwargs)
            return ok, mock_post

    def test_emoji_title_is_latin1_safe_and_rfc2047(self):
        ok, mock_post = self._send(title="⚡ Carrac data source alert")
        assert ok is True
        headers = mock_post.call_args.kwargs["headers"]
        # Must not blow up inside requests: every header value latin-1-encodable
        for v in headers.values():
            v.encode("latin-1")
        assert headers["Title"].startswith("=?UTF-8?B?")

    def test_ascii_title_passes_through_unchanged(self):
        ok, mock_post = self._send(title="Carrac data source alert")
        assert ok is True
        assert mock_post.call_args.kwargs["headers"]["Title"] == "Carrac data source alert"

    def test_emoji_tags_encoded(self):
        ok, mock_post = self._send(title="x", tags="zap,⚡")
        assert ok is True
        headers = mock_post.call_args.kwargs["headers"]
        for v in headers.values():
            v.encode("latin-1")

    def test_body_utf8_and_send_succeeds(self):
        ok, mock_post = self._send(title="⚡ alert")
        assert ok is True
        body = mock_post.call_args.kwargs["data"]
        assert isinstance(body, bytes) and "⚡".encode("utf-8") in body


# ---------------------------------------------------------------------------
# (f) notify=False — chronic, fallback-covered sources stay silent
# ---------------------------------------------------------------------------

class TestNotifyFlag:
    """The stooq breaker is chronic (intermittent IP block of this host) and
    fully Yahoo-covered — its mechanism-level pushes were pure noise (62 OPEN
    transitions / 48h, multiplied per process). notify=False keeps the breaker
    opening/probing/logging but never pushing."""

    def test_notify_false_suppresses_open_push(self):
        with patch(_NOTIFY_PATH) as mock_notify:
            b = CircuitBreaker("quiet", threshold=1, notify=False)
            b.record_failure()
            assert b.is_tripped
            mock_notify.assert_not_called()

    def test_notify_false_suppresses_trip_push(self):
        with patch(_NOTIFY_PATH) as mock_notify:
            b = CircuitBreaker("quiet_trip", threshold=3, notify=False)
            b.trip()
            assert b.is_tripped
            mock_notify.assert_not_called()

    def test_is_tripped_is_read_only(self):
        """Unlike is_open(), is_tripped must not release the half-open probe."""
        # notify=False: this test is about is_tripped's read-only semantics, not
        # pushes. Without it the breaker fired a real "⚡ Carrac data source
        # alert" on every run — caught by the notification-send guard.
        b = CircuitBreaker("probe_src", threshold=1, half_open_after=0.0,
                           notify=False)
        b.record_failure()
        assert b.is_tripped
        # The probe is still available to is_open() afterwards
        assert b.is_open() is False  # half_open_after=0 → probe released here


# ---------------------------------------------------------------------------
# (g) Impact-level TradFi outage alert — stooq AND Yahoo both down
# ---------------------------------------------------------------------------

class TestTradfiOutageAlert:
    """Replaces the silenced stooq mechanism alert: push ONCE (6h per-process
    cooldown) only when stooq AND Yahoo breakers are both open — i.e. TradFi
    data is actually unavailable."""

    @staticmethod
    def _reset_cooldown():
        import data_sources.stooq as stooq_mod
        stooq_mod._impact_state["last_notified_at"] = None

    @staticmethod
    def _client(stooq_tripped: bool, yahoo_open: bool):
        from data_sources.stooq import StooqClient
        breaker = CircuitBreaker("stooq_impact_test", threshold=1, notify=False)
        if stooq_tripped:
            breaker.trip()
        yahoo = MagicMock()
        yahoo.daily_breaker.is_open.return_value = yahoo_open
        return StooqClient(breaker, yahoo)

    def test_both_breakers_open_pushes_once(self):
        self._reset_cooldown()
        client = self._client(stooq_tripped=True, yahoo_open=True)
        with patch("carrac.notifications.ntfy.send_ntfy") as mock_send:
            df1 = client._yahoo_fallback("SPX", None)
            df2 = client._yahoo_fallback("GLD", None)  # within cooldown
            assert df1.empty and df2.empty
            assert mock_send.call_count == 1
            assert "stooq AND Yahoo" in mock_send.call_args.kwargs["body"]

    def test_yahoo_open_but_stooq_healthy_no_push(self):
        # Parse-error fallbacks land here with a healthy stooq breaker —
        # per-symbol issue, not a TradFi outage.
        self._reset_cooldown()
        client = self._client(stooq_tripped=False, yahoo_open=True)
        with patch("carrac.notifications.ntfy.send_ntfy") as mock_send:
            df = client._yahoo_fallback("SPX", None)
            assert df.empty
            mock_send.assert_not_called()

    def test_yahoo_healthy_uses_fallback_no_push(self):
        self._reset_cooldown()
        client = self._client(stooq_tripped=True, yahoo_open=False)
        with patch("carrac.notifications.ntfy.send_ntfy") as mock_send:
            client._yahoo_fallback("SPX", None)
            client.yahoo.fetch_daily.assert_called_once()
            mock_send.assert_not_called()
