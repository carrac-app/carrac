"""Slow-request WARNING tests — Flask side.

Verifies that requests exceeding SLOW_REQUEST_THRESHOLD_S emit a 'SLOW'
WARNING via the 'carrac' logger, and that fast requests do not.
"""

import logging



class TestFlaskSlowRequestLog:
    """Flask after_request hook fires SLOW warning when elapsed > threshold."""

    def test_slow_request_emits_warning(self, client, monkeypatch, caplog):
        """With threshold patched to 0.0 every request is 'slow' → WARNING fires.

        Uses GET /login — a cheap Flask page route guaranteed present in the
        test client (unlike /health which is FastAPI-only in the strangler app).
        """
        import carrac as _carrac

        monkeypatch.setattr(_carrac, "SLOW_REQUEST_THRESHOLD_S", 0.0)

        with caplog.at_level(logging.WARNING, logger="carrac"):
            client.get("/login")

        slow_records = [
            r for r in caplog.records
            if r.name == "carrac" and r.levelno == logging.WARNING and "SLOW" in r.getMessage()
        ]
        assert slow_records, (
            f"Expected a 'SLOW' WARNING in the 'carrac' logger but none found. "
            f"All carrac WARNING records: "
            f"{[r.getMessage() for r in caplog.records if r.name == 'carrac' and r.levelno == logging.WARNING]}"
        )
        msg = slow_records[0].getMessage()
        assert "GET" in msg
        assert "/login" in msg
        assert "status=" in msg

    def test_fast_request_no_warning(self, client, monkeypatch, caplog):
        """With threshold set very high no SLOW warning fires."""
        import carrac as _carrac

        monkeypatch.setattr(_carrac, "SLOW_REQUEST_THRESHOLD_S", 9999.0)

        with caplog.at_level(logging.WARNING, logger="carrac"):
            client.get("/login")

        slow_records = [
            r for r in caplog.records
            if r.name == "carrac" and r.levelno == logging.WARNING and "SLOW" in r.getMessage()
        ]
        assert not slow_records, (
            f"Did not expect a 'SLOW' WARNING but found: {[r.getMessage() for r in slow_records]}"
        )
