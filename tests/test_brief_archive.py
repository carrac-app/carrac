"""Public brief permalinks + archive — Task B.

Honest-permalink contract:
``/brief/<date>`` renders the STORED ``general`` report for that calendar
date in a simple reader layout — never live bands, never a personal splice,
never an operator-private section. ``/brief/archive`` lists the last 30
general reports (date + tldr) linking to their dated permalinks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import re
import pytest


@pytest.fixture(autouse=True)
def _hosted_site_url(monkeypatch):
    """These tests pin page structure/copy against the hosted instance's
    address; the generated links follow SITE_URL, so say which instance."""
    monkeypatch.setenv("SITE_URL", "https://example.test")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


# Carries an operator-private section (must be stripped by
# strip_operator_sections) AND an inline personal-section splice marker
# (must be truncated before the marker) — same fixture shape as
# tests/test_brief_public.py, exercised here against the DATED route
# instead of the live one.
_GENERAL_WITH_LEAKS = (
    "## The Story\nNeutral overview for the archive.\n\n"
    "## Action Dashboard\n\U0001f534 **BTC Position Risk** — Short from $75,736 is +6.2% in-the-money.\n\n"
    "<!-- PERSONAL_SECTION_START -->\n## The Story\nPersonalized content — must never reach a public visitor.\n"
)

_GENERAL_NEUTRAL = (
    "## TL;DR\nMarkets held a tight range into the FOMC blackout window.\n\n"
    "## The Story\nNeutral shared macro overview for the daily brief.\n\n"
    "## Market Snapshot\n| Asset | Price |\n| BTC | 71287 |\n"
)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Force get_redis() to return None so every test renders live by
    default; tests that want to exercise the cache path override this."""
    import carrac.redis_cache as rc
    monkeypatch.setattr(rc, 'get_redis', lambda: None)


@pytest.fixture
def db(app):  # noqa: ARG001 — app fixture sets DATA_DIR before we bind the singleton
    """ContentDatabase singleton bound to THIS test's isolated DATA_DIR.

    ``get_content_db()`` is a process-global singleton that freezes its path
    on first use; reset it so rows seeded here land in the same DB the
    routes read (mirrors tests/fastapi/test_trade_shares_asgi.py's
    ``trade_db`` fixture).
    """
    import carrac.content_db as cdb

    cdb._db_instance = None
    database = cdb.get_content_db()
    yield database
    cdb._db_instance = None


def _seed_general_on(db, date_str, content, summary=None):
    """Insert a 'general' report row directly, backdated to ``date_str``.

    ``save_report`` is idempotent per (report_type, UTC calendar day) — every
    call stamps ``generation_date`` as "today", so it can never seed more
    than one row per real day. Archive/permalink tests need several DISTINCT
    simulated days in one run, so this bypasses the upsert with a direct
    INSERT (SQLite only — mirrors ``_backdate_report`` in
    tests/test_report_security_and_features.py, a pattern already blessed
    in this codebase for exactly this kind of date-shifted test fixture).
    """
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO reports "
        "(report_type, content, assets_included, generation_time_ms, summary, generation_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ('general', content, '[]', 0, summary, date_str, f"{date_str} 06:00:00"),
    )
    conn.commit()


def _today_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _days_ago_str(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime('%Y-%m-%d')


class TestBriefDatedRoute:
    def test_dated_report_renders_200_with_content(self, client, db):
        date = _days_ago_str(1)
        _seed_general_on(db, date, _GENERAL_NEUTRAL, summary='Markets held a tight range.')
        resp = client.get(f'/brief/{date}')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert date in html
        assert 'window.__briefData' in html
        assert 'Neutral shared macro overview' in html
        assert 'not financial advice' in html.lower()

    def test_unknown_date_404s(self, client, db):  # noqa: ARG002 — db ensures isolated ContentDatabase
        resp = client.get('/brief/2020-01-01')
        assert resp.status_code == 404

    def test_no_reports_at_all_404s(self, client, db):  # noqa: ARG002
        resp = client.get(f'/brief/{_today_str()}')
        assert resp.status_code == 404

    @pytest.mark.parametrize('bad_date', [
        'not-a-date',
        '2026-13-40',
        '2026/07/02',
        '20260702',
        '../../etc/passwd',
        '2026-02-30',  # syntactically date-shaped, calendar-invalid
    ])
    def test_malformed_date_404s(self, client, db, bad_date):  # noqa: ARG002
        resp = client.get(f'/brief/{bad_date}')
        assert resp.status_code == 404

    def test_future_date_404s(self, client, db):  # noqa: ARG002
        future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%d')
        resp = client.get(f'/brief/{future}')
        assert resp.status_code == 404

    def test_operator_sections_and_personal_marker_absent(self, client, db):
        """Same CONTRACT as /brief: the personal-section marker must never
        reach the rendered response, and the operator-private Action
        Dashboard section must be stripped from the inlined report content —
        for an archived date, not just "latest"."""
        date = _days_ago_str(2)
        _seed_general_on(db, date, _GENERAL_WITH_LEAKS)
        resp = client.get(f'/brief/{date}')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert '<!-- PERSONAL_SECTION_START -->' not in html
        assert 'Personalized content' not in html
        assert '## Action Dashboard' not in html
        assert '75,736' not in html

    def test_no_live_bands_on_dated_page(self, client, db):
        """Honest-permalink guarantee: the dated reader must never inline the
        live-band gather blobs (sentiment/breadth/snapshot) /brief uses —
        only the stored report content."""
        date = _days_ago_str(1)
        _seed_general_on(db, date, _GENERAL_NEUTRAL)
        resp = client.get(f'/brief/{date}')
        html = resp.get_data(as_text=True)
        assert 'ed-ribbon' not in html
        assert 'ed-docket' not in html
        assert 'ed-liquidity' not in html

    def test_second_get_served_from_cache(self, client, db, monkeypatch):
        """Mirrors /brief's own cache test: the first request renders live
        and populates the per-date Redis key; the second must be served from
        that cached HTML (proven by mutating the DB between requests)."""
        date = _days_ago_str(1)
        _seed_general_on(db, date, _GENERAL_NEUTRAL)

        store: dict[str, bytes] = {}

        class _FakeRedis:
            def get(self, key):
                return store.get(key)

            def setex(self, key, ttl, value):
                store[key] = value.encode() if isinstance(value, str) else value

        import carrac.redis_cache as rc
        monkeypatch.setattr(rc, 'get_redis', lambda: _FakeRedis())

        r1 = client.get(f'/brief/{date}')
        assert r1.status_code == 200
        # See the note in tests/test_brief_public.py — version bumps are a
        # correct change and must not fail this test.
        from carrac.reports.brief_archive import _DATED_CACHE_PREFIX
        assert f'{_DATED_CACHE_PREFIX}{date}' in store

        conn = db._get_conn()
        conn.execute(
            "UPDATE reports SET content = ? WHERE report_type = 'general' AND generation_date = ?",
            ("## The Story\nCHANGED-AFTER-CACHE-SET — must not appear.\n", date),
        )
        conn.commit()

        r2 = client.get(f'/brief/{date}')
        assert r2.status_code == 200
        def _strip_nonce(body):
            return re.sub(r'nonce="[^"]*"', 'nonce=""', body)

        assert (_strip_nonce(r2.get_data(as_text=True))
                == _strip_nonce(r1.get_data(as_text=True)))
        assert b'CHANGED-AFTER-CACHE-SET' not in r2.data

        # Inline scripts on the cache hit carry THIS response's nonce.
        csp = r2.headers.get('Content-Security-Policy', '')
        m = re.search(r"'nonce-([^']+)'", csp)
        assert m, csp
        assert set(re.findall(r'nonce="([^"]*)"', r2.get_data(as_text=True))) == {m.group(1)}

    def test_cache_error_falls_through_to_live_render(self, client, db, monkeypatch):
        """A broken Redis (raises on get/set) must be non-fatal — the route
        still renders live instead of 500ing."""
        date = _days_ago_str(1)
        _seed_general_on(db, date, _GENERAL_NEUTRAL)

        class _BrokenRedis:
            def get(self, key):
                raise ConnectionError('redis unavailable')

            def setex(self, key, ttl, value):
                raise ConnectionError('redis unavailable')

        import carrac.redis_cache as rc
        monkeypatch.setattr(rc, 'get_redis', lambda: _BrokenRedis())

        resp = client.get(f'/brief/{date}')
        assert resp.status_code == 200
        assert 'window.__briefData' in resp.get_data(as_text=True)


class TestBriefArchiveRoute:
    def test_lists_seeded_dates_with_tldr(self, client, db):
        d1, d2 = _days_ago_str(1), _days_ago_str(2)
        _seed_general_on(db, d1, _GENERAL_NEUTRAL, summary='Markets held a tight range.')
        _seed_general_on(db, d2, "## The Story\nA different day's overview entirely.\n")

        resp = client.get('/brief/archive')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert d1 in html
        assert d2 in html
        assert f'/brief/{d1}' in html
        assert f'/brief/{d2}' in html
        assert 'Markets held a tight range' in html

    def test_empty_archive_renders_200(self, client, db):  # noqa: ARG002
        resp = client.get('/brief/archive')
        assert resp.status_code == 200
        assert 'No archived briefs yet' in resp.get_data(as_text=True)

    def test_operator_sections_and_personal_marker_absent_from_tldr(self, client, db):
        date = _days_ago_str(1)
        _seed_general_on(db, date, _GENERAL_WITH_LEAKS)
        resp = client.get('/brief/archive')
        html = resp.get_data(as_text=True)
        assert '<!-- PERSONAL_SECTION_START -->' not in html
        assert 'Personalized content' not in html
        assert '75,736' not in html

    def test_licensed_prose_absent_from_the_stored_summary(self, client, db):
        _seed_general_on(db, _days_ago_str(1), _GENERAL_NEUTRAL,
                         summary='Markets held a tight range. DVOL sits at cycle lows.')
        html = client.get('/brief/archive').get_data(as_text=True)
        assert 'Markets held a tight range' in html
        assert 'DVOL' not in html


class TestSitemapIncludesBriefArchive:
    def test_brief_archive_in_sitemap(self):
        """/brief/archive must be listed in the FastAPI sitemap.xml page list
        (see carrac/fastapi_app/routers/migrated.py::sitemap_xml). Called
        directly — it's a plain sync function with no request dependencies —
        mirroring TestSitemapIncludesBrief in tests/test_brief_public.py."""
        from carrac.fastapi_app.routers.migrated import sitemap_xml
        resp = sitemap_xml()
        body = resp.body
        if isinstance(body, (bytes, bytearray)):
            body = body.decode('utf-8')
        assert 'https://example.test/brief/archive' in body


class TestBlueprintRegistration:
    def test_brief_archive_blueprint_registered(self, app):
        assert 'brief_archive' in app.blueprints
