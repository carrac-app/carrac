"""The live /api/report/morning/latest never hands a non-operator the
operator's summary or narration.

A paid caller with no 'general' row yet is served the morning report through
the operator-neutral filter. The content was filtered; the summary (the
operator's personal executive summary) and audio_script (narration written
from the full text) must not ride along unfiltered.
"""

from __future__ import annotations

_HDRS = {"X-Internal-Key": "test-api-key-for-testing"}
_SUMMARY = "Operator summary: hold the BTC short, stop $79,872."
_SCRIPT = "Your BTC short from $75,736 is working; keep the stop at $79,872."
_MORNING = {
    "content": (
        "## The Story\nMacro narrative.\n\n"
        "## Action Dashboard\nShort entered at $75,736 with stop $79,872.\n\n"
        "## Market Snapshot\n| BTC | 71287 |\n"
    ),
    "created_at": "2026-06-10",
    "id": 7,
    "summary": _SUMMARY,
    "audio_script": _SCRIPT,
    "metadata": None,
}


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def get_latest_report(self, report_type=None, max_age_days=None):  # noqa: ARG002
        return self._rows.get(report_type)

    def get_personal_section(self, report_id, user_id):  # noqa: ARG002
        return None


def _serve(asgi_client, monkeypatch, tier):
    from carrac import content_db as cdb
    import carrac.fastapi_app.routers.reports as rr

    monkeypatch.setenv("ENABLE_MORNING_REPORT", "true")
    monkeypatch.setattr(cdb, "get_content_db", lambda: _FakeDB({"morning": _MORNING}))
    monkeypatch.setattr(rr, "get_request_tier", lambda request: tier)
    r = asgi_client.get("/api/report/morning/latest", headers=_HDRS)
    assert r.status_code == 200
    return r.json()


def test_paid_fallback_withholds_operator_summary_and_narration(asgi_client, monkeypatch):
    data = _serve(asgi_client, monkeypatch, "premium")
    assert "Market Snapshot" in data["content"]
    blob = " ".join(str(data.get(k) or "") for k in ("content", "summary", "audio_script"))
    assert "Action Dashboard" not in blob
    assert "75,736" not in blob
    assert "79,872" not in blob


def test_operator_still_gets_summary_and_narration(asgi_client, monkeypatch):
    data = _serve(asgi_client, monkeypatch, "admin")
    assert "Action Dashboard" in data["content"]
    assert data["summary"] == _SUMMARY
    assert data["audio_script"] == _SCRIPT
