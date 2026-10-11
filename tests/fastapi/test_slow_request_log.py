"""Slow-request WARNING tests — FastAPI side.

Verifies that requests exceeding SLOW_REQUEST_THRESHOLD_S emit a 'SLOW'
WARNING via the 'carrac.fastapi_app.main' logger, and that fast requests
do not.
"""

import logging


_LOGGER_NAME = "carrac.fastapi_app.main"


class TestFastAPISlowRequestLog:
    """FastAPI SlowRequestMiddleware fires SLOW warning when elapsed > threshold."""

    def test_slow_request_emits_warning(self, asgi_client, monkeypatch, caplog):
        """With threshold patched to 0.0 every request is 'slow' → WARNING fires.

        Patches ``_carrac_pkg.SLOW_REQUEST_THRESHOLD_S`` on the main module
        because the middleware reads it as an attribute reference at call time
        (not a frozen local copy).
        """
        import carrac.fastapi_app.main as _fa_main

        monkeypatch.setattr(_fa_main._carrac_pkg, "SLOW_REQUEST_THRESHOLD_S", 0.0)

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            resp = asgi_client.get("/health")

        assert resp.status_code == 200
        slow_records = [
            r for r in caplog.records
            if r.name == _LOGGER_NAME
            and r.levelno == logging.WARNING
            and "SLOW" in r.getMessage()
        ]
        assert slow_records, (
            f"Expected a 'SLOW' WARNING in '{_LOGGER_NAME}' but none found. "
            f"All {_LOGGER_NAME} WARNING records: "
            f"{[r.getMessage() for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.WARNING]}"
        )
        msg = slow_records[0].getMessage()
        assert "GET" in msg
        assert "/health" in msg
        assert "status=" in msg

    def test_fast_request_no_warning(self, asgi_client, monkeypatch, caplog):
        """With threshold set very high no SLOW warning fires."""
        import carrac.fastapi_app.main as _fa_main

        monkeypatch.setattr(_fa_main._carrac_pkg, "SLOW_REQUEST_THRESHOLD_S", 9999.0)

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            resp = asgi_client.get("/health")

        assert resp.status_code == 200
        slow_records = [
            r for r in caplog.records
            if r.name == _LOGGER_NAME
            and r.levelno == logging.WARNING
            and "SLOW" in r.getMessage()
        ]
        assert not slow_records, (
            f"Did not expect a 'SLOW' WARNING but found: {[r.getMessage() for r in slow_records]}"
        )
