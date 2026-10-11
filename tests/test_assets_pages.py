"""Tests for carrac/assets/public_pages.py — the public /assets page helpers.

The security-critical surface is validate_symbol(): /assets/<symbol> feeds the
path straight at the Redis facts cache, so arbitrary input must be rejected
(whitelist against the curated registry) before it ever reaches a cache key.
"""

from __future__ import annotations

import pytest

from unittest.mock import patch

from carrac.assets.public_pages import allowed_symbols, validate_symbol, clustered_assets


@pytest.fixture(autouse=True)
def _hosted_site_url(monkeypatch):
    """These tests pin page structure/copy against the hosted instance's
    address; the generated links follow SITE_URL, so say which instance."""
    monkeypatch.setenv("SITE_URL", "https://example.test")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


def test_allowed_symbols_are_curated_non_ratio():
    syms = allowed_symbols()
    assert "BTC" in syms and "GOLD" in syms and "SPX" in syms
    # ratios get no standalone page
    assert "GOLD_QQQ" not in syms and "MAGS_SPX" not in syms and "QQQ_SPX" not in syms
    assert len(syms) == len(set(syms))  # no dupes


def test_validate_symbol_accepts_known_and_normalizes_case():
    assert validate_symbol("BTC") == "BTC"
    assert validate_symbol("btc") == "BTC"      # case-normalized
    assert validate_symbol("  eth ") == "ETH"   # trimmed


def test_validate_symbol_rejects_unknown():
    assert validate_symbol("FOOBAR") is None
    assert validate_symbol("GOLD_QQQ") is None   # a ratio — not a page


def test_validate_symbol_rejects_malicious_input():
    # path traversal / injection / overflow vectors must never pass the whitelist
    for bad in ["../etc/passwd", "BTC/../ETH", "BTC;DROP TABLE", "<script>",
                "BTC%00", "A" * 64, "", "   ", "BTC OR 1=1", "facts:bundle:*"]:
        assert validate_symbol(bad) is None, bad


def test_clustered_assets_groups_by_registry_cluster():
    groups = clustered_assets()
    names = [g["cluster"] for g in groups]
    assert "Crypto" in names
    # every listed symbol is whitelisted + carries a display name + has no ratios
    for g in groups:
        for a in g["assets"]:
            assert validate_symbol(a["symbol"]) == a["symbol"]
            assert a["name"]


# ─────────────────────────────── routes (security + render) ────────────────────

_STUB_BTC = {"symbol": "BTC", "price": 60000.0, "as_of": "2026-06-29T08:08",
             "ma_stack": {"state": "bearish", "bars_held": 7}}


class TestAssetsRoutes:
    """The public surface: gating (ships dark), the symbol whitelist (404 on
    anything off-list — the security boundary), render, noindex, and sitemap."""

    def test_dark_returns_404_for_anonymous(self, client, monkeypatch):
        monkeypatch.delenv("ASSETS_PAGES_PUBLIC", raising=False)
        assert client.get("/assets").status_code == 404
        assert client.get("/assets/BTC").status_code == 404

    def test_admin_can_preview_while_dark(self, client, monkeypatch):
        monkeypatch.delenv("ASSETS_PAGES_PUBLIC", raising=False)
        with client.session_transaction() as s:
            s["is_admin"] = True
        resp = client.get("/assets")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Tracked assets" in body
        assert "noindex" in body  # dark → not indexable

    def test_public_flag_serves_directory_indexable(self, client, monkeypatch):
        monkeypatch.setenv("ASSETS_PAGES_PUBLIC", "1")
        resp = client.get("/assets")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "/assets/BTC" in body          # links to permalinks
        assert "noindex" not in body          # public → indexable

    def test_directory_has_og_meta_and_cta(self, client, monkeypatch):
        """The sitemap'd directory carried zero og:/twitter: tags
        and no signup CTA — anonymous (unfurl-scraper-shaped) request."""
        monkeypatch.setenv("ASSETS_PAGES_PUBLIC", "1")
        resp = client.get("/assets")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert 'property="og:image" content="https://example.test/static/og-dashboard.png"' in body
        assert 'property="og:title"' in body
        assert 'property="og:url" content="https://example.test/assets"' in body
        assert 'name="twitter:card" content="summary_large_image"' in body
        assert "utm_source=assets&amp;utm_medium=seo&amp;utm_campaign=index" in body
        assert "Get started free" in body

    def test_detail_unknown_symbol_is_404(self, client, monkeypatch):
        monkeypatch.setenv("ASSETS_PAGES_PUBLIC", "1")
        assert client.get("/assets/FOOBAR").status_code == 404
        assert client.get("/assets/GOLD_QQQ").status_code == 404   # a ratio, not a page

    def test_detail_known_symbol_renders_and_normalizes_case(self, client, monkeypatch):
        monkeypatch.setenv("ASSETS_PAGES_PUBLIC", "1")
        with patch("carrac.facts.wire.get_asset_facts", return_value=_STUB_BTC):
            resp = client.get("/assets/btc")   # lower-case path → normalized to BTC
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Bitcoin" in body and "BTC" in body
        # canonical points at the normalized, https marketing URL
        assert 'rel="canonical" href="https://example.test/assets/BTC"' in body
        # non-advice posture holds on the public page
        for banned in ("you should", "i recommend", "i would"):
            assert banned not in body.lower()
        # per-symbol OG + CTA
        assert 'property="og:title" content="BTC — live indicator readout · Carrac"' in body
        assert 'property="og:image" content="https://example.test/static/og-dashboard.png"' in body
        assert "utm_source=assets&amp;utm_medium=seo&amp;utm_campaign=btc" in body
        assert "Get started free" in body

    def test_detail_cache_miss_does_not_500(self, client, monkeypatch):
        monkeypatch.setenv("ASSETS_PAGES_PUBLIC", "1")
        with patch("carrac.facts.wire.get_asset_facts", side_effect=RuntimeError("no redis")):
            resp = client.get("/assets/ETH")
        assert resp.status_code == 200
        assert "No data available" in resp.data.decode("utf-8")

    def test_assets_in_sitemap_only_when_public(self, monkeypatch):
        # /sitemap.xml is FastAPI-migrated (strangler MIGRATED_PATHS) — the
        # Flask twin no longer exists (it was shadowed in production), so
        # this drives the live handler directly rather than the Flask `client`.
        from carrac.fastapi_app.routers.migrated import sitemap_xml as live_sitemap_xml
        monkeypatch.delenv("ASSETS_PAGES_PUBLIC", raising=False)
        assert "/assets/BTC" not in live_sitemap_xml().body.decode("utf-8")
        monkeypatch.setenv("ASSETS_PAGES_PUBLIC", "1")
        body = live_sitemap_xml().body.decode("utf-8")
        assert "/assets</loc>" in body or "/assets<" in body
        assert "/assets/BTC" in body
