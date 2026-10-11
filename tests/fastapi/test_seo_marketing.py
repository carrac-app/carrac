"""SEO assertions for the marketing surface — rendered through the
strangler ASGI app (Flask fall-through for pages, FastAPI for sitemap)."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _own_site_url(monkeypatch):
    monkeypatch.setenv("SITE_URL", "https://example.test")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


class TestCentralOgBlock:
    # /contact is a static page (no data deps) — a reliable canary that the
    # base-template default now emits self-derived OG/Twitter tags. The fix
    # is in the base, so it applies to every page that doesn't override og.
    def test_contact_emits_self_derived_og(self, asgi_client):
        resp = asgi_client.get("/contact")
        assert resp.status_code == 200
        html = resp.text
        assert 'property="og:title"' in html
        assert 'property="og:description"' in html
        assert 'property="og:url"' in html
        assert 'name="twitter:title"' in html
        assert 'name="twitter:description"' in html
        # values self-derived from the page's own title/canonical blocks
        assert 'og:title" content="Contact' in html
        assert 'og:url" content="https://example.test/contact"' in html


class TestTitleFormat:
    @pytest.mark.parametrize("path", ["/indicators", "/formulas", "/changelog"])  # public pages
    def test_no_bracket_artifact_in_title(self, asgi_client, path):
        html = asgi_client.get(path).text
        start = html.find("<title>")
        end = html.find("</title>")
        title = html[start:end]
        assert "[Carrac" not in title  # no bracket wordmark in <title>
        assert "Carrac" in title
        assert "Carrac.cc" not in title  # the hosted domain is not the brand


class TestNoindex:
    @pytest.mark.parametrize("path", ["/login", "/register"])
    def test_auth_pages_noindex(self, asgi_client, path):
        html = asgi_client.get(path).text
        assert 'name="robots"' in html and "noindex" in html


class TestGuideSchema:
    def test_guide_has_jsonld(self, asgi_client):
        html = asgi_client.get("/guide").text
        assert 'application/ld+json' in html
        assert '"TechArticle"' in html

    def test_guide_twitter_large_card(self, asgi_client):
        html = asgi_client.get("/guide").text
        assert 'twitter:card" content="summary_large_image"' in html


class TestSitemapEntries:
    def test_sitemap_has_lastmod(self, asgi_client):
        xml = asgi_client.get("/sitemap.xml").text
        assert "<lastmod>" in xml

    def test_journal_guide_absent(self, asgi_client):
        xml = asgi_client.get("/sitemap.xml").text
        assert "/journal/guide" not in xml


class TestChangelogSSR:
    """The changelog is PUBLIC (it is sitemap'd + robots.txt-allowed, so a
    login gate would send every anonymous visitor/crawler a 302->/login). SSR content
    assertions still run through the authed Flask client (behavior is
    identical either way — the template has no auth-conditional content)."""

    def test_changelog_public_anonymous_200(self, asgi_client):
        resp = asgi_client.get("/changelog", follow_redirects=False)
        assert resp.status_code == 200

    def test_changelog_anonymous_shows_recent_entry(self, asgi_client):
        """An anonymous visitor (no session cookie at all — this
        client never logs in) must see real changelog content server-side,
        not a login wall."""
        from changelog import CHANGELOG
        html = asgi_client.get("/changelog").text
        assert CHANGELOG[0]["version"] in html
        assert CHANGELOG[0]["title"] in html

    def test_changelog_renders_latest_version_server_side(self, auth_client):
        from changelog import APP_VERSION
        html = auth_client.get("/changelog").get_data(as_text=True)
        # The current version string must be in the server-rendered HTML,
        # not only injected later by client JS.
        assert APP_VERSION in html

    def test_changelog_renders_multiple_entries_server_side(self, auth_client):
        """Latest N changelog entries must be present in the initial HTML payload."""
        from changelog import CHANGELOG
        html = auth_client.get("/changelog").get_data(as_text=True)
        # At least the 3 most recent entries must be server-rendered
        for entry in CHANGELOG[:3]:
            assert entry["version"] in html
            assert entry["title"] in html

    def test_changelog_entry_changes_are_server_rendered(self, auth_client):
        """Change list items for the latest entry must appear in the HTML."""
        from changelog import CHANGELOG
        html = auth_client.get("/changelog").get_data(as_text=True)
        latest = CHANGELOG[0]
        for change in latest["changes"]:
            assert change in html


class TestJournalNav:
    @pytest.mark.parametrize("path", ["/", "/contact"])
    def test_no_gated_journal_guide_link(self, asgi_client, path):
        html = asgi_client.get(path).text
        assert 'href="/journal/guide"' not in html

    def test_guide_has_journal_anchor(self, asgi_client):
        html = asgi_client.get("/guide").text
        assert 'id="journal"' in html


class TestMcpPage:
    def test_mcp_page_renders(self, asgi_client):
        resp = asgi_client.get("/mcp")
        assert resp.status_code == 200
        html = resp.text
        assert "<title>" in html and "MCP" in html
        assert 'rel="canonical" href="https://example.test/mcp"' in html
        assert 'property="og:title"' in html

    def test_mcp_in_sitemap(self, asgi_client):
        assert "/mcp" in asgi_client.get("/sitemap.xml").text


class TestMarketRegimePage:
    def test_renders(self, asgi_client):
        resp = asgi_client.get("/market-regime")
        assert resp.status_code == 200
        html = resp.text
        assert 'rel="canonical" href="https://example.test/market-regime"' in html
        assert "<h1" in html

    def test_in_sitemap(self, asgi_client):
        assert "/market-regime" in asgi_client.get("/sitemap.xml").text


class TestInternalLinks:
    @pytest.mark.parametrize("path", ["/markets", "/guide"])
    def test_new_pages_linked_in_footer(self, asgi_client, path):
        html = asgi_client.get(path).text
        assert 'href="/mcp"' in html
        assert 'href="/market-regime"' in html


class TestGoogleSiteVerification:
    def test_gsc_file_served_at_root(self, asgi_client):
        resp = asgi_client.get("/googleda32e4dd9422762a.html")
        assert resp.status_code == 200
        assert "google-site-verification: googleda32e4dd9422762a.html" in resp.text
