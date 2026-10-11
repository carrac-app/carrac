"""The report TEXT route must bound the age of 'morning' too.

`GET /api/report/morning/latest` (FastAPI, strangler MIGRATED_PATHS — the live
front door) had two unbounded `get_latest_report('morning')` calls:

  * the ``is_operator`` branch — safe only because ENABLE_MORNING_REPORT is
    false in production, i.e. a feature flag doing a freshness check's job;
  * the paid-tier existence fallback — fires when 'general' has ZERO rows, and
    would have served a 46-day-old report to paid users. ``operator_neutral()``
    scrubs the operator's positions but not the *age*, so the content was still
    June's market read presented as today's.

Sibling of the prior-day staleness primitive and the latest-audio route (the
same bug, live). These tests drive the REAL FastAPI handler; only tier
resolution is patched, which is the request's identity, not the unit under
test.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport

_MORNING = (
    "## The Story\nJune's macro narrative.\n\n"
    "## Action Dashboard\n"
    "\U0001f534 **BTC Position Risk** — Short from $75,736 is +6.2% in-the-money.\n"
)
_GENERAL = "## The Story\nToday's neutral shared macro overview.\n"


@pytest.fixture
def db(app):  # noqa: ARG001 — app fixture sets DATA_DIR before we bind the singleton
    import carrac.content_db as cdb

    cdb._db_instance = None
    database = cdb.get_content_db()
    yield database
    cdb._db_instance = None


@pytest.fixture
def reports_app(app):  # noqa: ARG001
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware

    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.deps.auth import RateContext, auth_or_key, classify_request
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.reports import router as reports_router

    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(test_app)
    test_app.include_router(reports_router)
    test_app.dependency_overrides[classify_request] = lambda: RateContext(
        pool="web_session", key="user:test", tier="free")
    test_app.dependency_overrides[auth_or_key] = lambda: {"user_id": None}
    return test_app


@pytest.fixture
def reports_client(reports_app):
    ac = httpx.AsyncClient(
        transport=ASGITransport(app=reports_app), base_url="http://testserver")

    class _Sync:
        def __init__(self, c):
            self._c = c
            self._loop = asyncio.new_event_loop()

        def __getattr__(self, n):
            m = getattr(self._c, n)
            return lambda *a, **k: self._loop.run_until_complete(m(*a, **k))

        def close(self):
            self._loop.run_until_complete(self._c.aclose())
            self._loop.close()

    w = _Sync(ac)
    yield w
    w.close()


def _seed(db, report_type, days_ago, content):
    """Insert a report with a REAL created_at in the past.

    save_report() always stamps now, so the raw insert is the only way to
    reproduce the 46-day-old row that made this a live bug on the audio route.
    """
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    conn = db._get_conn()
    cur = conn.execute(
        "INSERT INTO reports "
        "(report_type, content, assets_included, generation_time_ms, summary, "
        "generation_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (report_type, content, '[]', 0, None, when.strftime('%Y-%m-%d'),
         when.strftime('%Y-%m-%d %H:%M:%S')),
    )
    conn.commit()
    return cur.lastrowid


def _as_tier(tier, personal_report):
    return (
        patch("carrac.fastapi_app.routers.reports.get_request_tier",
              return_value=tier),
        patch("carrac.auth.decorators.get_tier_limits",
              return_value={"personal_report": personal_report}),
    )


def _content(resp):
    return ((resp.json() or {}).get("content") or "") if resp.status_code == 200 else ""


class TestPaidTierFallbackIsAgeBounded:
    """The fallback that would have shipped June's report to paying users."""

    def test_stale_morning_is_not_served_as_the_paid_fallback(self, reports_client, db):
        # Prod's exact shape if 'general' ever gapped: 'morning' frozen 46 days
        # ago, no 'general' row at all.
        _seed(db, "morning", days_ago=46, content=_MORNING)
        p1, p2 = _as_tier("premium", "full")
        with p1, p2:
            r = reports_client.get("/api/report/morning/latest")
        assert "June's macro narrative" not in _content(r), \
            "a 46-day-old report was served to a paid user as the latest report"

    def test_fresh_morning_is_still_served_as_the_paid_fallback(self, reports_client, db):
        """The bound must not kill the feature — otherwise the bound just
        swaps one defect for another."""
        _seed(db, "morning", days_ago=0, content=_MORNING)
        p1, p2 = _as_tier("premium", "full")
        with p1, p2:
            r = reports_client.get("/api/report/morning/latest")
        assert r.status_code == 200, r.text
        content = _content(r)
        assert "June's macro narrative" in content
        # …and still operator-neutral, as before the change.
        assert "75,736" not in content

    def test_fresh_general_still_wins_over_stale_morning(self, reports_client, db):
        _seed(db, "morning", days_ago=46, content=_MORNING)
        _seed(db, "general", days_ago=0, content=_GENERAL)
        p1, p2 = _as_tier("premium", "full")
        with p1, p2:
            r = reports_client.get("/api/report/morning/latest")
        assert r.status_code == 200, r.text
        assert "Today's neutral shared macro overview" in _content(r)


class TestOperatorBranchIsAgeBounded:
    """ENABLE_MORNING_REPORT was the only thing keeping this safe."""

    def test_admin_with_stale_morning_falls_through_to_general(
            self, reports_client, db, monkeypatch):
        # Flip the flag ON — the exact config change that must not
        # silently start serving a months-old report.
        monkeypatch.setenv("ENABLE_MORNING_REPORT", "true")
        _seed(db, "morning", days_ago=46, content=_MORNING)
        _seed(db, "general", days_ago=0, content=_GENERAL)
        p1, p2 = _as_tier("admin", "full")
        with p1, p2:
            r = reports_client.get("/api/report/morning/latest")
        assert r.status_code == 200, r.text
        content = _content(r)
        assert "Today's neutral shared macro overview" in content
        assert "June's macro narrative" not in content

    def test_admin_fallthrough_is_sanitised_like_any_shared_reader(
            self, reports_client, db, monkeypatch):
        """The fall-through downgrades the request to the shared path, so the
        response must go through the sanitiser — not keep operator privileges
        while reading a shared report."""
        monkeypatch.setenv("ENABLE_MORNING_REPORT", "true")
        _seed(db, "morning", days_ago=46, content=_MORNING)
        _seed(db, "general", days_ago=0,
              content=_GENERAL + "\n<!-- PERSONAL_SECTION_START -->\nprivate\n")
        p1, p2 = _as_tier("admin", "full")
        with p1, p2:
            r = reports_client.get("/api/report/morning/latest")
        assert r.status_code == 200, r.text
        assert "PERSONAL_SECTION_START" not in _content(r)

    def test_admin_with_fresh_morning_still_gets_it(self, reports_client, db, monkeypatch):
        monkeypatch.setenv("ENABLE_MORNING_REPORT", "true")
        _seed(db, "morning", days_ago=0, content=_MORNING)
        p1, p2 = _as_tier("admin", "full")
        with p1, p2:
            r = reports_client.get("/api/report/morning/latest")
        assert r.status_code == 200, r.text
        content = _content(r)
        assert "June's macro narrative" in content
        # Operator view is NOT sanitised — the admin still sees their own book.
        assert "75,736" in content


class TestStaleMorningAlarms:
    """Withholding a stale report is reported, never silent."""

    def test_withholding_a_stale_report_beacons_to_bugsink(
            self, reports_client, db, monkeypatch):
        monkeypatch.setenv("ENABLE_MORNING_REPORT", "true")
        _seed(db, "morning", days_ago=46, content=_MORNING)
        _seed(db, "general", days_ago=0, content=_GENERAL)

        captured = []
        import carrac.fastapi_app.routers.reports as R
        monkeypatch.setattr(
            R, "_stale_morning_alarm", lambda ctx: captured.append(ctx))

        p1, p2 = _as_tier("admin", "full")
        with p1, p2:
            reports_client.get("/api/report/morning/latest")
        assert captured, "the route withheld a stale report and said nothing"

    def test_a_missing_morning_report_does_not_alarm(
            self, reports_client, db, monkeypatch):
        """Only a row that EXISTS but is too old is a stalled pipeline. No row
        at all is a fresh install — alarming there trains you to ignore the
        alarm."""
        monkeypatch.setenv("ENABLE_MORNING_REPORT", "true")
        _seed(db, "general", days_ago=0, content=_GENERAL)

        captured = []
        import carrac.fastapi_app.routers.reports as R
        monkeypatch.setattr(
            R, "_stale_morning_alarm", lambda ctx: captured.append(ctx))

        p1, p2 = _as_tier("admin", "full")
        with p1, p2:
            reports_client.get("/api/report/morning/latest")
        assert not captured, f"alarmed on an absent report: {captured}"
