"""
Tests for editorial asset URL cache-busting.

Verifies that report-editorial.css and report-fonts.css links carry ?v=<hash>
so each deploy produces a new URL → guaranteed cache miss in both the browser
HTTP cache and the service worker stale-while-revalidate cache.

Three test layers:
1. Unit — cache_version() helper returns a stable hash for a real file.
2. Structural — template source contains the ?v={{ cache_version(...) }} expression.
3. Integration — facts_showcase rendered page carries ?v=<hash> in the href.
"""
import re
import pytest
from pathlib import Path


TEMPLATES_DIR = Path(__file__).parent.parent / 'templates'


# ---------------------------------------------------------------------------
# Layer 1 — cache_version helper (unit)
# ---------------------------------------------------------------------------

class TestCacheVersionHelper:
    """cache_version() is a Jinja context-processor; verify via the app fixture."""

    def _get_cache_version(self, app):
        """Extract the cache_version callable from the context processors."""
        with app.test_request_context('/'):
            for fn in app.template_context_processors[None]:
                ctx = fn()
                if 'cache_version' in ctx:
                    return ctx['cache_version']
        pytest.fail("cache_version context processor not registered on the app")

    def test_returns_nonempty_hash_for_existing_css(self, app):
        """cache_version('css/report-editorial.css') returns a non-empty hash string."""
        cv = self._get_cache_version(app)
        v = cv('css/report-editorial.css')
        assert v, "cache_version returned empty value for an existing CSS file"
        # Should look like an 8-char hex hash (md5[:8]), not '0' and not a timestamp
        assert len(v) >= 6, f"Unexpectedly short version: {v!r}"

    def test_returns_nonempty_hash_for_fonts_css(self, app):
        """cache_version('css/report-fonts.css') returns a non-empty hash string."""
        cv = self._get_cache_version(app)
        v = cv('css/report-fonts.css')
        assert v, "cache_version returned empty value for report-fonts.css"

    def test_returns_fallback_for_missing_file(self, app):
        """cache_version returns a non-empty fallback (timestamp) for a missing file."""
        cv = self._get_cache_version(app)
        v = cv('css/this-file-does-not-exist.css')
        # The existing implementation returns str(int(time.time())) on OSError
        assert v, "cache_version returned empty for missing file"


# ---------------------------------------------------------------------------
# Layer 2 — Structural template checks
# ---------------------------------------------------------------------------

class TestEditorialTemplateStructure:
    """Template source must contain ?v={{ cache_version(...) }} on the CSS links."""

    def _template_source(self, name):
        return (TEMPLATES_DIR / name).read_text()

    def test_morning_report_editorial_fonts_versioned(self):
        src = self._template_source('morning_report_editorial.html')
        assert "report-fonts.css" in src, "report-fonts.css not in editorial template at all"
        # The href must be followed by ?v={{ cache_version(...) }} on the same line
        assert any(
            "report-fonts.css" in line and "cache_version" in line and "?v=" in line
            for line in src.splitlines()
        ), "report-fonts.css link is missing the ?v={{ cache_version(...) }} expression"

    def test_morning_report_editorial_editorial_css_versioned(self):
        src = self._template_source('morning_report_editorial.html')
        assert "report-editorial.css" in src, "report-editorial.css not in editorial template at all"
        assert any(
            "report-editorial.css" in line and "cache_version" in line and "?v=" in line
            for line in src.splitlines()
        ), "report-editorial.css link is missing the ?v={{ cache_version(...) }} expression"

    def test_asset_detail_fonts_versioned(self):
        src = self._template_source('asset_detail.html')
        assert any(
            "report-fonts.css" in line and "cache_version" in line and "?v=" in line
            for line in src.splitlines()
        ), "asset_detail report-fonts.css link is missing the ?v={{ cache_version(...) }} expression"

    def test_asset_detail_editorial_css_versioned(self):
        src = self._template_source('asset_detail.html')
        assert any(
            "report-editorial.css" in line and "cache_version" in line and "?v=" in line
            for line in src.splitlines()
        ), "asset_detail report-editorial.css link is missing the ?v={{ cache_version(...) }} expression"


# ---------------------------------------------------------------------------
# Layer 3 — Integration: rendered facts_showcase page
# ---------------------------------------------------------------------------

class TestFactsShowcaseRenderedVersioning:
    """The facts_showcase page (real HTTP response) carries ?v=<hash> in CSS hrefs."""

    @pytest.fixture(autouse=True)
    def _stub_side_modules(self, monkeypatch):
        """Prevent Postgres/Redis timeouts (same stubs as test_facts_showcase.py)."""
        try:
            import carrac.facts.correlations as _corr_mod
            monkeypatch.setattr(
                _corr_mod, 'get_conn',
                lambda: (_ for _ in ()).throw(RuntimeError("no DB in tests")),
            )
        except Exception:
            pass
        try:
            import quant.api.routes as _qr
            monkeypatch.setattr(_qr, 'build_cot', lambda: None)
        except Exception:
            pass
        try:
            import carrac.grounding.facts_block as _fb
            monkeypatch.setattr(_fb, 'render_macro_facts_strip', lambda *a, **kw: "")
        except Exception:
            pass

    def test_rendered_css_links_carry_version(self, client, monkeypatch):
        """GET /assets/BTC → HTML contains report-editorial.css?v=<hash>."""
        from unittest.mock import patch
        monkeypatch.setenv('ASSETS_PAGES_PUBLIC', '1')
        with patch('carrac.facts.wire.get_asset_facts', return_value=None):
            resp = client.get('/assets/BTC')

        assert resp.status_code == 200
        html = resp.data.decode('utf-8')

        # report-editorial.css must carry ?v=<something>
        m = re.search(r'report-editorial\.css\?v=([^"\'&\s]+)', html)
        assert m, "Rendered asset_detail: report-editorial.css link is missing ?v=<hash>"
        assert m.group(1), "?v= present but value is empty"

        # report-fonts.css must also carry ?v=<something>
        m2 = re.search(r'report-fonts\.css\?v=([^"\'&\s]+)', html)
        assert m2, "Rendered asset_detail: report-fonts.css link is missing ?v=<hash>"
        assert m2.group(1), "report-fonts.css ?v= present but value is empty"
