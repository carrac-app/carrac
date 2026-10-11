"""Public marketing/legal pages carry the instance's own address, never the
retired hosted domain (canonical, og, JSON-LD, mail links, brand text)."""
import pytest

BASE = "https://example.test"

PAGES = [
    "/", "/api/docs",
    "/brief/archive", "/changelog",
    "/contact", "/disclaimer", "/formulas", "/guide", "/indicators",
    "/integrations", "/intelligence", "/login",
    "/market-regime", "/markets", "/mcp", "/register",
    "/workflow", "/terms", "/privacy",
]

# Allowlist: /changelog's BODY quotes historical release notes (frozen text,
# which legitimately mention a retired domain), so only its <head> is checked.
# /brief (hits live data sources; asserted in tests/test_brief_public.py), /assets pages and the logged-in journal guide are covered below / in
# tests/test_assets_pages.py.
HEAD_ONLY = {"/changelog"}


@pytest.fixture(autouse=True)
def _own_site_url(monkeypatch):
    monkeypatch.setenv("SITE_URL", BASE)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("SUPPORT_EMAIL", raising=False)


@pytest.mark.parametrize("path", PAGES)
def test_page_has_no_hosted_domain(asgi_client, path):
    r = asgi_client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    text = r.text.split("</head>")[0] if path in HEAD_ONLY else r.text
    assert "carrac.cc" not in text, path


def test_journal_guide_has_no_hosted_domain(auth_client):
    r = auth_client.get("/journal/guide")
    assert r.status_code == 200
    assert "carrac.cc" not in r.get_data(as_text=True)
