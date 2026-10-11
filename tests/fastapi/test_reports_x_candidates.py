"""x_candidates field surfaced from metadata via the LIVE FastAPI handler.

The route is FastAPI-migrated (strangler MIGRATED_PATHS): drives
``asgi_client`` to verify the real live handler, not the shadowed Flask twin.
"""

from __future__ import annotations

import json


_GENERAL_CONTENT = "## The Story\nNeutral overview.\n"
_ADMIN_HDRS = {"X-Internal-Key": "test-api-key-for-testing"}


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def get_latest_report(self, report_type=None):
        return self._rows.get(report_type)

    def get_personal_section(self, report_id, user_id):
        return None


def _patch_db(monkeypatch, rows):
    from carrac import content_db as cdb
    monkeypatch.setattr(cdb, "get_content_db", lambda: _FakeDB(rows))


def _patch_tier(monkeypatch, tier):
    import carrac.fastapi_app.routers.reports as rr
    monkeypatch.setattr(rr, "get_request_tier", lambda request: tier)


def _general_row(candidates=None):
    meta = json.dumps({"x_candidates": candidates}) if candidates is not None else None
    return {
        "content": _GENERAL_CONTENT,
        "created_at": "2026-06-10",
        "id": 50,
        "summary": None,
        "audio_script": None,
        "metadata": meta,
    }


class TestXCandidatesLiveHandler:
    """x_candidates in metadata must appear in the JSON response for
    non-operator callers on the LIVE FastAPI handler."""

    def test_free_tier_gets_x_candidates(self, asgi_client, monkeypatch):
        candidates = ["Thread 1", "Thread 2", "Thread 3"]
        _patch_db(monkeypatch, {"general": _general_row(candidates)})
        _patch_tier(monkeypatch, "free")
        r = asgi_client.get("/api/report/morning/latest", headers=_ADMIN_HDRS)
        assert r.status_code == 200
        data = r.json() or {}
        assert "x_candidates" in data
        assert data["x_candidates"] == candidates

    def test_paid_tier_gets_x_candidates(self, asgi_client, monkeypatch):
        candidates = ["Thread A", "Thread B"]
        _patch_db(monkeypatch, {"general": _general_row(candidates)})
        _patch_tier(monkeypatch, "premium")
        r = asgi_client.get("/api/report/morning/latest", headers=_ADMIN_HDRS)
        assert r.status_code == 200
        data = r.json() or {}
        assert "x_candidates" in data
        assert data["x_candidates"] == candidates

    def test_x_candidates_absent_when_not_in_metadata(self, asgi_client, monkeypatch):
        _patch_db(monkeypatch, {"general": _general_row(candidates=None)})
        _patch_tier(monkeypatch, "free")
        r = asgi_client.get("/api/report/morning/latest", headers=_ADMIN_HDRS)
        assert r.status_code == 200
        data = r.json() or {}
        # Key should not appear when metadata has no x_candidates
        assert not data.get("x_candidates")

    def test_admin_as_general_gets_x_candidates(self, asgi_client, monkeypatch):
        candidates = ["Thread X"]
        morning_row = {
            "content": "## The Story\nMorning operator.\n",
            "created_at": "2026-06-10", "id": 1,
            "summary": None, "audio_script": None, "metadata": None,
        }
        _patch_db(monkeypatch, {
            "morning": morning_row,
            "general": _general_row(candidates),
        })
        _patch_tier(monkeypatch, "admin")
        r = asgi_client.get("/api/report/morning/latest?as=general", headers=_ADMIN_HDRS)
        assert r.status_code == 200
        data = r.json() or {}
        assert "x_candidates" in data
        assert data["x_candidates"] == candidates
