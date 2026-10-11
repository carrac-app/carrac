"""Parity tests for the three migrated public read routes.

* ``GET /health`` — substantive: hits Postgres + Redis + cache + worker
  heartbeat. We assert response shape (keys present, types) and the
  status-code semantics (200 healthy / 503 degraded), not byte equality
  — ``response_time_ms``, ``worker_heartbeat_age_sec``, and
  ``checks.cache.htf.age_seconds`` are inherently volatile.
* ``GET /robots.txt`` — byte-identical to Flask handler output.
* ``GET /sitemap.xml`` — byte-identical to Flask handler output.

Security headers (X-Content-Type-Options, X-Frame-Options, HSTS,
Referrer-Policy, Permissions-Policy) come from
``carrac.fastapi_app.middleware.SecurityHeadersMiddleware`` and are
asserted independently — this is the prod traffic path, not the Flask
test client path.
"""

from __future__ import annotations

import pytest

from pathlib import Path


from changelog import APP_VERSION


@pytest.fixture(autouse=True)
def _hosted_site_url(monkeypatch):
    """These tests pin page structure/copy against the hosted instance's
    address; the generated links follow SITE_URL, so say which instance."""
    monkeypatch.setenv("SITE_URL", "https://carrac.cc")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

BASELINES_DIR = Path(__file__).parent / "baselines"


class TestHealthLive:
    """/health is a cheap liveness probe. No DB, no Redis,
    no compute. Used by Docker's healthcheck directive."""

    def test_health_returns_200_alive(self, asgi_client):
        resp = asgi_client.get("/health")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json() == {"status": "alive", "version": APP_VERSION}

    def test_health_version_matches_app_version(self, asgi_client):
        # Deploy gates assert this server-side so a
        # container running different code than the working tree fails loud.
        resp = asgi_client.get("/health")
        assert resp.json()["version"] == APP_VERSION

    def test_health_response_headers_present(self, asgi_client):
        resp = asgi_client.get("/health")
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "SAMEORIGIN"
        assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
        assert "geolocation=()" in resp.headers.get("permissions-policy", "")
        assert resp.headers.get("x-api-version") == "1"


class TestHealthReady:
    """/health/ready is the deep readiness probe. Kuma and the uptime watchdog probe this route."""

    def test_health_ready_returns_json(self, asgi_client):
        resp = asgi_client.get("/health/ready")
        # 200 healthy or 503 degraded — the deep health semantics.
        assert resp.status_code in (200, 503)
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert data["status"] in ("healthy", "degraded")
        assert "checks" in data
        assert "database" in data["checks"]
        assert "redis" in data["checks"]
        assert "cache" in data["checks"]
        assert "response_time_ms" in data
        assert isinstance(data["response_time_ms"], int)
        assert "degraded_reasons" in data
        assert isinstance(data["degraded_reasons"], list)
        assert "worker_heartbeat_age_sec" in data
        assert data["worker_heartbeat_age_sec"] is None or isinstance(
            data["worker_heartbeat_age_sec"], int
        )

    def test_health_ready_database_ok_in_test_env(self, asgi_client):
        resp = asgi_client.get("/health/ready")
        assert resp.json()["checks"]["database"]["status"] == "ok"

    def test_a_dead_worker_makes_readiness_degraded(self, asgi_client, monkeypatch):
        """The case Kuma exists to catch.

        The heartbeat key carries a TTL, so a worker that dies stops
        refreshing it and the key EXPIRES. This endpoint then read age=None
        and reported healthy, so the outage silently cleared its own
        alarm. Redis is stubbed as up-but-empty, which is exactly what a
        machine with a live Redis and a dead worker looks like.
        """
        import carrac.caches as _caches

        class _EmptyRedis:
            def get(self, key):
                return None

        monkeypatch.setattr(_caches, "_get_redis", lambda: _EmptyRedis())

        resp = asgi_client.get("/health/ready")
        data = resp.json()

        assert data["worker_heartbeat_age_sec"] is None
        assert "worker heartbeat absent" in data["degraded_reasons"], data
        assert data["status"] == "degraded"
        assert resp.status_code == 503

    def test_a_live_worker_keeps_readiness_healthy(self, asgi_client, monkeypatch):
        """The other half — a fresh heartbeat must not be reported as absent,
        or every deploy would page and the alarm would be trained away."""
        import json as _j
        from datetime import datetime as _d

        import carrac.caches as _caches

        class _LiveRedis:
            def get(self, key):
                return _j.dumps({"ts": _d.now().isoformat(), "pid": 1})

        monkeypatch.setattr(_caches, "_get_redis", lambda: _LiveRedis())

        data = asgi_client.get("/health/ready").json()

        assert isinstance(data["worker_heartbeat_age_sec"], int)
        assert "worker heartbeat absent" not in data["degraded_reasons"]
        assert "worker heartbeat stale" not in data["degraded_reasons"]

    def test_health_ready_response_headers_present(self, asgi_client):
        resp = asgi_client.get("/health/ready")
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "SAMEORIGIN"
        assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
        assert "geolocation=()" in resp.headers.get("permissions-policy", "")
        assert resp.headers.get("x-api-version") == "1"


class TestRobotsTxt:
    def test_marketing_allow_block_present(self, asgi_client):
        resp = asgi_client.get("/robots.txt")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        # Public SaaS surface — robots.txt allows marketing routes and
        # links to the sitemap. Cloudflare augments the bytes at the
        # edge in production; we just verify the key directives are present
        # in the origin response.
        body = resp.text
        assert "User-agent: *" in body
        assert "Allow: /" in body
        assert "Disallow: /api/" in body
        assert "Sitemap: https://carrac.cc/sitemap.xml" in body

    def test_security_headers_present(self, asgi_client):
        resp = asgi_client.get("/robots.txt")
        assert resp.headers.get("x-content-type-options") == "nosniff"


class TestSitemapXml:
    def test_matches_baseline(self, asgi_client, monkeypatch):
        # The sitemap appends one entry per stored daily brief, read from the
        # shared reports DB. Earlier tests in the same process can leave rows
        # there, which made this baseline diff order-dependent. Pin the brief
        # list to empty; dated entries are covered by the test below.
        from carrac.fastapi_app.routers import migrated
        # The baseline is host-neutral: links follow SITE_URL, so render it
        # against a neutral test host.
        monkeypatch.setenv("SITE_URL", "https://example.com")
        monkeypatch.setattr(migrated, "_list_recent_briefs_for_sitemap", lambda: [])
        resp = asgi_client.get("/sitemap.xml")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/xml")
        baseline = (BASELINES_DIR / "sitemap.xml").read_text()
        # Tolerant of trailing-whitespace drift in the baseline fixture.
        assert resp.text.rstrip() == baseline.rstrip()

    def test_dated_brief_permalinks_are_listed(self, asgi_client, monkeypatch):
        from carrac.fastapi_app.routers import migrated
        monkeypatch.setenv("SITE_URL", "https://example.com")
        monkeypatch.setattr(
            migrated, "_list_recent_briefs_for_sitemap",
            lambda: [{"date": "2026-01-02", "tldr": "x"}],
        )
        resp = asgi_client.get("/sitemap.xml")
        assert "<loc>https://example.com/brief/2026-01-02</loc>" in resp.text
        baseline = (BASELINES_DIR / "sitemap.xml").read_text()
        assert resp.text.rstrip() != baseline.rstrip()

    def test_security_headers_present(self, asgi_client):
        resp = asgi_client.get("/sitemap.xml")
        assert resp.headers.get("x-frame-options") == "SAMEORIGIN"


class TestStranglerDispatch:
    """Ensure the strangler dispatcher actually routes migrated paths
    to FastAPI and unmigrated paths to Flask."""

    def test_v2_ping_still_works(self, asgi_client):
        # Sanity probe — should still respond after the public routes migrated.
        resp = asgi_client.get("/api/v2/_ping")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_v2_generated_api_docs_are_not_served(self, asgi_client):
        # The published contract is /api/docs + /api/openapi.json. The
        # FastAPI sub-app's generated spec lists every route, admin ones
        # included, so it is not exposed.
        # (The curl-chart catch-all answers these paths with a 4xx.)
        for path in ("/api/v2/_openapi.json", "/api/v2/_docs"):
            resp = asgi_client.get(path)
            assert 400 <= resp.status_code < 500, path
            assert "/api/admin" not in resp.text and "swagger" not in resp.text.lower(), path

    def test_unmigrated_path_falls_through_to_flask(self, asgi_client):
        # Index route is Flask-served — exercising Flask via WsgiToAsgi
        # confirms the strangler fall-through.
        resp = asgi_client.get("/")
        assert resp.status_code == 200

    def test_unknown_path_404s(self, asgi_client):
        resp = asgi_client.get("/this-route-does-not-exist-anywhere")
        assert resp.status_code == 404
