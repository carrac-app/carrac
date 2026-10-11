"""/api/report/morning/latest tier gating — tested through the LIVE handler.

The route is FastAPI-migrated (strangler MIGRATED_PATHS): the Flask twin is
shadowed in production, so its in-process tests can be green while the live handler
leaks. Pins against a demo leak: a premium API key was served
the operator's morning report — Action Dashboard with live entries/stops.

Contract: the operator's morning report is ADMIN-ONLY. Every other tier gets
the operator-neutral shared "general" report; paid tiers fall back to a
NEUTRALIZED morning (sections stripped + position lines scrubbed) when no
general row exists, so they are never served an empty report.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


_MORNING_OPERATOR = (
    "## The Story\nMacro is heavy.\n"
    "Long ETH entered at $2,251 — underwater.\n\n"
    "## Action Dashboard\n"
    "- BTC short entered at $75,736 with stop $79,872.\n\n"
    "## Scenarios\nRange break favors continuation.\n"
)
_GENERAL = "## The Story\nGENERAL-SHARED-VIEW\n\n## Market Snapshot\n| BTC | 100 |\n"

_ADMIN_HDRS = {"X-Internal-Key": "test-api-key-for-testing"}


class _FakeDB:
    """Stands in for ContentDatabase, and must MODEL it, not merely satisfy it.

    ``max_age_days`` landed on the real ``get_latest_report`` later and
    this stub never grew it, so a bounded call raised TypeError inside the
    route. Worse, had it simply swallowed the kwarg, these tests would stay
    green whether or not the freshness bound worked at all — a mock sitting
    above the thing it is supposed to prove.

    So it enforces the bound the same way the real method does: a row older
    than ``max_age_days`` is not "the latest report", it is nothing.
    """

    def __init__(self, rows):
        self._rows = rows

    def get_latest_report(self, report_type=None, max_age_days=None):
        row = self._rows.get(report_type)
        if row is None or max_age_days is None:
            return row
        created = datetime.fromisoformat(row["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
        return None if age_days > max_age_days else row

    def get_personal_section(self, report_id, user_id):
        return None


def _patch_db(monkeypatch, rows):
    from carrac import content_db as cdb

    monkeypatch.setattr(cdb, "get_content_db", lambda: _FakeDB(rows))


def _patch_tier(monkeypatch, tier):
    import carrac.fastapi_app.routers.reports as rr

    monkeypatch.setattr(rr, "get_request_tier", lambda request: tier)


# Rows are CURRENT. These tests are about tier gating, so the reports must be
# fresh enough to be servable at all — otherwise the freshness bound
# decides the outcome and the gating assertions prove nothing. (Pinned
# dates would go stale.)
# Staleness itself is covered by
# tests/fastapi/test_morning_text_staleness.py.
_NOW = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

_MORNING_ROW = {
    "content": _MORNING_OPERATOR, "created_at": _NOW, "id": 1,
    "summary": None, "audio_script": None, "metadata": None,
}
_GENERAL_ROW = {
    "content": _GENERAL, "created_at": _NOW, "id": 2,
    "summary": None, "audio_script": None, "metadata": None,
}


class TestMorningLatestLiveHandlerGating:
    def test_premium_with_general_row_gets_general(self, asgi_client, monkeypatch):
        _patch_db(monkeypatch, {"morning": _MORNING_ROW, "general": _GENERAL_ROW})
        _patch_tier(monkeypatch, "premium")
        r = asgi_client.get("/api/report/morning/latest", headers=_ADMIN_HDRS)
        assert r.status_code == 200
        content = (r.json() or {}).get("content") or ""
        assert "GENERAL-SHARED-VIEW" in content
        assert "Action Dashboard" not in content
        assert "75,736" not in content

    def test_premium_no_general_gets_neutralized_morning(self, asgi_client, monkeypatch):
        _patch_db(monkeypatch, {"morning": _MORNING_ROW})
        _patch_tier(monkeypatch, "premium")
        r = asgi_client.get("/api/report/morning/latest", headers=_ADMIN_HDRS)
        assert r.status_code == 200
        content = (r.json() or {}).get("content") or ""
        assert content.strip(), "paid tier must not be served an empty report"
        assert "Action Dashboard" not in content
        assert "75,736" not in content
        assert "entered at $2,251" not in content
        # neutral market prose survives the scrub
        assert "Macro is heavy" in content
        assert "Range break favors continuation" in content

    def test_free_no_general_gets_empty_not_morning(self, asgi_client, monkeypatch):
        _patch_db(monkeypatch, {"morning": _MORNING_ROW})
        _patch_tier(monkeypatch, "free")
        r = asgi_client.get("/api/report/morning/latest", headers=_ADMIN_HDRS)
        assert r.status_code == 200
        assert (r.json() or {}).get("content") in (None, "")

    def test_admin_gets_full_morning(self, asgi_client, monkeypatch):
        _patch_db(monkeypatch, {"morning": _MORNING_ROW, "general": _GENERAL_ROW})
        _patch_tier(monkeypatch, "admin")
        r = asgi_client.get("/api/report/morning/latest", headers=_ADMIN_HDRS)
        assert r.status_code == 200
        content = (r.json() or {}).get("content") or ""
        assert "Action Dashboard" in content
        assert "75,736" in content
        assert "GENERAL-SHARED-VIEW" not in content

    def test_admin_as_general_previews_neutralized(self, asgi_client, monkeypatch):
        # ?as=general lets an admin preview the shared general report exactly as a
        # non-personalized user sees it (QA + a clean public source for social
        # auto-posting): positions stripped, never the operator's morning book.
        _patch_db(monkeypatch, {"morning": _MORNING_ROW, "general": _GENERAL_ROW})
        _patch_tier(monkeypatch, "admin")
        r = asgi_client.get("/api/report/morning/latest?as=general", headers=_ADMIN_HDRS)
        assert r.status_code == 200
        content = (r.json() or {}).get("content") or ""
        assert "GENERAL-SHARED-VIEW" in content
        assert "Action Dashboard" not in content
        assert "75,736" not in content
