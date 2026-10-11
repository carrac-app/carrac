"""Sitemap, robots.txt and llms.txt are rooted at the instance's SITE_URL."""
import pytest

BASE = "https://example.test"


@pytest.fixture(autouse=True)
def _own_site_url(monkeypatch):
    monkeypatch.setenv("SITE_URL", BASE)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


def test_sitemap_robots_llms(asgi_client):
    for path in ("/sitemap.xml", "/robots.txt", "/llms.txt"):
        r = asgi_client.get(path)
        assert r.status_code == 200, path
        assert BASE in r.text, path
        assert "carrac.cc" not in r.text, path


