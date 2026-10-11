"""Public `/brief` route — the daily GENERAL report, no login
required, served on the full editorial page.

Package A backend contract: the route reuses `_editorial_context()` (the SAME gather
`morning_report()` uses in editorial mode) and the SAME as=general
shaping/sanitization the authed `/api/report/morning/latest` API uses
(`carrac.reports.sanitize.strip_operator_sections`) — it must never leak the
operator's private sections, and it must render for anonymous AND logged-in
visitors identically (no auth branch at all).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hosted_site_url(monkeypatch):
    """These tests pin page structure/copy against the hosted instance's
    address; the generated links follow SITE_URL, so say which instance."""
    monkeypatch.setenv("SITE_URL", "https://example.test")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


_GENERAL_NEUTRAL = (
    "## The Story\nNeutral shared macro overview for the daily brief.\n\n"
    "## Market Snapshot\n| Asset | Price |\n| BTC | 71287 |\n"
)

# Carries an operator-private section (must be stripped by
# strip_operator_sections) AND an inline personal-section splice marker
# (must be truncated before the marker) — exercises both defenses at once.
_GENERAL_WITH_LEAKS = (
    "## The Story\nNeutral overview.\n\n"
    "## Action Dashboard\n\U0001f534 **BTC Position Risk** — Short from $75,736 is +6.2% in-the-money.\n\n"
    "<!-- PERSONAL_SECTION_START -->\n## The Story\nPersonalized content — must never reach a public visitor.\n"
)


def _seed_general(content=_GENERAL_NEUTRAL):
    """Seed a 'general' report row via the same DB helper the real engine
    and the authed API's handler use (carrac.content_db)."""
    from carrac.content_db import get_content_db
    return get_content_db().save_report(
        content=content, assets_included=[], generation_time_ms=0,
        report_type='general',
    )


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Force get_redis() to return None so every test renders live by
    default; tests that want to exercise the cache path override this."""
    import carrac.redis_cache as rc
    monkeypatch.setattr(rc, 'get_redis', lambda: None)


@pytest.fixture(autouse=True)
def _isolated_content_db(app):  # noqa: ARG001 — app fixture sets DATA_DIR before we bind the singleton
    """``get_content_db()`` is a process-global singleton that freezes its
    path on first use — reset it so the 'general' reports ``_seed_general``
    inserts here land in (and are torn down with) THIS test's isolated
    DATA_DIR, instead of leaking into whichever test file happens to call
    ``get_content_db()`` next in the same pytest session (mirrors the ``db``
    fixture in tests/test_brief_archive.py)."""
    import carrac.content_db as cdb
    cdb._db_instance = None
    yield
    cdb._db_instance = None


@pytest.fixture(autouse=True)
def _fast_quant_db_miss(monkeypatch):
    """The quant Postgres pool (`quant.*` / `carrac.facts.bands.*` — used by
    `_editorial_context()` for docket/liquidity/breadth/rotation/flows/COT
    and by `_gather_brief_inline()` for the snapshot blob) isn't reachable in
    this sqlite-only local test environment. Without this, every one of those
    gather steps burns real wall-clock time on a DNS-resolution retry before
    its try/except falls back to None/[] — this short-circuits the pool init
    so the suite runs at unit-test speed. The code path under test (graceful
    degradation on a DB miss) is unaffected; only the time-to-fail is."""
    import carrac.db_pool as db_pool

    def _raise(*a, **k):
        raise RuntimeError('no quant DB in this test environment')

    monkeypatch.setattr(db_pool, 'init_quant_pool', _raise)


def _strip_nonce(html: str) -> str:
    import re
    return re.sub(r'nonce="[^"]*"', 'nonce=""', html)


class TestBriefPublicRoute:
    def test_anonymous_get_200_with_brief_markers(self, client):
        _seed_general()
        resp = client.get('/brief')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'Daily Brief' in html
        assert 'window.__briefData' in html
        assert 'not financial advice' in html.lower()
        assert 'https://example.test/brief' in html
        assert 'carrac.cc' not in html

    def test_operator_sections_and_personal_marker_absent(self, client):
        """CONTRACT: the personal-section marker must never reach the
        rendered response, and the operator-private Action Dashboard section
        must be stripped from the inlined report content."""
        _seed_general(_GENERAL_WITH_LEAKS)
        resp = client.get('/brief')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert '<!-- PERSONAL_SECTION_START -->' not in html
        assert 'Personalized content' not in html
        # "## Action Dashboard" (the markdown heading, i.e. actual report
        # content) must be stripped — NOT a bare "Action Dashboard" substring
        # check, which would false-positive on the template's own static JS
        # (a `sections.actionDashboard` fallback + an unrelated comment).
        assert '## Action Dashboard' not in html
        assert '75,736' not in html

    def test_logged_in_get_also_200_no_auth_branch(self, auth_client):
        """A logged-in (member-tier) session sees the exact same public
        brief — no personalization, no auth branching in the route."""
        _seed_general()
        resp = auth_client.get('/brief')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'Daily Brief' in html
        assert 'window.__briefData' in html

    def test_no_general_report_still_renders_200(self, client):
        """No 'general' row at all (cold DB) — brief_inline.report is None,
        but the page itself must never 500; the JS falls back to its own
        fetch (which degrades the same way it does today)."""
        resp = client.get('/brief')
        assert resp.status_code == 200
        assert 'window.__briefData' in resp.get_data(as_text=True)

    def test_second_get_served_from_cache(self, client, monkeypatch):
        """Monkeypatch redis get/set to observe the cache read/write without
        a real Redis: first request renders live and populates the cache;
        the second request must be served from that cached HTML, not a
        fresh render (proven by mutating the DB between requests and
        checking the second response is unaffected)."""
        _seed_general()
        store: dict[str, bytes] = {}

        class _FakeRedis:
            def get(self, key):
                return store.get(key)

            def setex(self, key, ttl, value):
                store[key] = value.encode() if isinstance(value, str) else value

        import carrac.redis_cache as rc
        monkeypatch.setattr(rc, 'get_redis', lambda: _FakeRedis())

        r1 = client.get('/brief')
        assert r1.status_code == 200
        # Imported, not hardcoded: the key carries a version that gets bumped
        # whenever cached HTML must be abandoned.
        # Asserting the literal made this test fail on a correct change.
        from carrac.reports.routes import _BRIEF_CACHE_KEY
        assert _BRIEF_CACHE_KEY in store

        # Mutate the DB — if the second GET re-rendered live, this string
        # would show up in the response. It must not.
        _seed_general(content="## The Story\nCHANGED-AFTER-CACHE-SET — must not appear.\n")

        r2 = client.get('/brief')
        assert r2.status_code == 200
        # Bodies match except for the per-request CSP nonce.
        assert (_strip_nonce(r2.get_data(as_text=True))
                == _strip_nonce(r1.get_data(as_text=True)))
        assert b'CHANGED-AFTER-CACHE-SET' not in r2.data

    def test_cached_hit_carries_current_request_nonce(self, client, monkeypatch):
        """Every inline script on a cache hit must carry the nonce of THAT
        response's CSP header, not the nonce of the request that filled the
        cache."""
        import re
        _seed_general()
        store: dict[str, bytes] = {}

        class _FakeRedis:
            def get(self, key):
                return store.get(key)

            def setex(self, key, ttl, value):
                store[key] = value.encode() if isinstance(value, str) else value

        import carrac.redis_cache as rc
        monkeypatch.setattr(rc, 'get_redis', lambda: _FakeRedis())

        client.get('/brief')
        r2 = client.get('/brief')
        csp = r2.headers.get('Content-Security-Policy', '')
        m = re.search(r"'nonce-([^']+)'", csp)
        assert m, csp
        nonces = re.findall(r'nonce="([^"]*)"', r2.get_data(as_text=True))
        assert nonces
        assert set(nonces) == {m.group(1)}

    def test_cache_error_falls_through_to_live_render(self, client, monkeypatch):
        """A broken Redis (raises on get/set) must be non-fatal — the route
        still renders live instead of 500ing."""
        _seed_general()

        class _BrokenRedis:
            def get(self, key):
                raise ConnectionError('redis unavailable')

            def setex(self, key, ttl, value):
                raise ConnectionError('redis unavailable')

        import carrac.redis_cache as rc
        monkeypatch.setattr(rc, 'get_redis', lambda: _BrokenRedis())

        resp = client.get('/brief')
        assert resp.status_code == 200
        assert 'window.__briefData' in resp.get_data(as_text=True)


class TestSitemapIncludesBrief:
    def test_brief_in_sitemap(self):
        """/brief must be listed in the FastAPI sitemap.xml page list (the
        live handler — see carrac/fastapi_app/routers/migrated.py::sitemap_xml,
        MIGRATED_PATHS strangler-routed). Called directly (it's a plain sync
        function with no request dependencies) rather than through the ASGI
        app, so this test stays self-contained in this file."""
        from carrac.fastapi_app.routers.migrated import sitemap_xml
        resp = sitemap_xml()
        body = resp.body
        if isinstance(body, (bytes, bytearray)):
            body = body.decode('utf-8')
        assert 'https://example.test/brief' in body


class TestBriefPaused:
    """With both report flags off, nothing produces a report any
    more. The public surfaces must SAY so instead of presenting the last stored
    report on the live editorial page as today's."""

    @pytest.fixture
    def paused(self, monkeypatch):
        monkeypatch.setenv('ENABLE_GENERAL_REPORT', 'false')
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'false')

    @pytest.fixture
    def running(self, monkeypatch):
        monkeypatch.setenv('ENABLE_GENERAL_REPORT', 'true')
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'false')

    def test_flag_reads_env_at_call_time(self, monkeypatch):
        from morning_engine.config import reports_paused
        monkeypatch.setenv('ENABLE_GENERAL_REPORT', 'true')
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'false')
        assert reports_paused() is False
        monkeypatch.setenv('ENABLE_GENERAL_REPORT', 'false')
        assert reports_paused() is True

    def test_brief_shows_paused_notice_not_live_page(self, client, paused):  # noqa: ARG002
        _seed_general()
        resp = client.get('/brief')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'data-brief-paused="1"' in html
        assert 'Automated briefs are paused' in html
        # the live editorial render (with its inlined JSON) must NOT be served
        assert 'window.__briefData' not in html
        # the archive list is there, so the stored report stays reachable
        assert '/brief/' in html

    def test_archive_carries_the_notice_when_paused(self, client, paused):  # noqa: ARG002
        _seed_general()
        html = client.get('/brief/archive').get_data(as_text=True)
        assert 'data-brief-paused="1"' in html
        assert "Today&rsquo;s Brief" not in html

    def test_running_pages_have_no_notice(self, client, running):  # noqa: ARG002
        _seed_general()
        assert 'data-brief-paused' not in client.get('/brief').get_data(as_text=True)
        html = client.get('/brief/archive').get_data(as_text=True)
        assert 'data-brief-paused' not in html
        assert "Today&rsquo;s Brief" in html

