"""A slow network must not leave the chart app with undefined globals.

1. static/service-worker.js never answers a script or stylesheet request with
   a synthetic 503 on timeout: the browser would parse the 503 body as the
   script, so none of that file's globals would ever exist. Scripts and
   styles wait for a real network outcome; images, fonts and the manifest
   keep the bounded fetch with the offline fallback.
2. The chart app's document-level click/touch/keydown handlers guard every
   read of a global owned by another file (klineContextMenu from
   kline-drawings.js, _freehandActive from freehand.js) with `typeof`, so one
   failed script cannot raise a ReferenceError on every later tap.
3. templates/index.html registers a capture-phase error listener before the
   first resource tag, so any script or stylesheet load failure is recorded
   in the carracLog ring buffer and sent to Sentry when it is loaded.

These are source-level checks; the suite has no headless-browser harness
for DOM behaviour (same pattern as test_mobile_sheet_teardown.py).
"""
import pathlib
import re

from tests.chart_app_sources import chart_app_source

REPO_ROOT = pathlib.Path(__file__).parents[1]
SW_JS = REPO_ROOT / "static" / "service-worker.js"
INDEX_HTML = REPO_ROOT / "templates" / "index.html"


def _sw_text():
    return SW_JS.read_text()


def _main_js_text():
    return chart_app_source()


def _index_html_text():
    return INDEX_HTML.read_text()


def _extract_between(text, start_marker, end_marker):
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


# ---------------------------------------------------------------------------
# Fix 1 — service-worker.js: never fake-503 an executable asset
# ---------------------------------------------------------------------------

class TestServiceWorkerExecutableAssetBranch:
    def _static_branch(self):
        """The static-asset stale-while-revalidate handler: from the
        isExecutableAsset declaration through the end of the fetch listener
        (bounded by the next top-level 'message' listener registration)."""
        js = _sw_text()
        return _extract_between(
            js,
            "var isExecutableAsset",
            "// Listen for messages from the app",
        )

    def test_is_executable_asset_checks_destination_and_extension_fallback(self):
        """isExecutableAsset must check request.destination for 'script'/'style'
        AND fall back to a URL-extension check for browsers that don't
        populate request.destination reliably."""
        js = _sw_text()
        m = re.search(r"var\s+isExecutableAsset\s*=([^;]+);", js, re.DOTALL)
        assert m, "isExecutableAsset is not declared in service-worker.js"
        decl = m.group(1)
        assert "event.request.destination === 'script'" in decl
        assert "event.request.destination === 'style'" in decl
        assert re.search(r"\\\.\(js\|css\)", decl), (
            "isExecutableAsset must fall back to a .js/.css URL-extension "
            "check for browsers that don't populate request.destination"
        )

    def test_script_style_path_has_no_timeout_wrapper(self):
        """RED-then-GREEN guard: no fetchWithTimeout() call anywhere inside
        the `if (isExecutableAsset) { ... }` block — the whole point of this
        fix is that a script/style fetch on this path is never bounded by a
        clock that can produce a synthetic 503. This must hold for BOTH the
        cache-hit background revalidation and the cache-miss fetch."""
        js = _sw_text()
        block = _extract_between(js, "if (isExecutableAsset) {", "        }\n\n        // Non-executable")
        assert "fetchWithTimeout" not in block, (
            "found fetchWithTimeout() inside the isExecutableAsset branch — "
            "script/style requests must never be raced against a clock "
            "that can produce a fake 503"
        )
        assert "fetch(event.request)" in block, (
            "the isExecutableAsset branch must fall through to a plain, "
            "un-timed-out fetch(event.request)"
        )

    def test_script_style_path_never_synthesizes_a_503(self):
        """No `new Response(...)` (synthetic response) anywhere in the
        executable-asset branch — a late script must beat a fake 503
        forever, never race it. (Checks actual code, not prose: this
        branch's own comments legitimately talk ABOUT not doing this.)"""
        js = _sw_text()
        block = _extract_between(js, "if (isExecutableAsset) {", "        }\n\n        // Non-executable")
        assert "new Response(" not in block, (
            "found a synthesized Response(...) inside the isExecutableAsset "
            "branch — script/style requests must never get a synthetic "
            "offline response; let a real network failure propagate instead"
        )

    def test_non_executable_assets_still_use_timeout_and_503_fallback(self):
        """Images/fonts/manifest.json etc. keep the
        bounded fetchWithTimeout + fake-503-on-miss behavior — only
        executable assets are exempted. Without this, a hung image request
        could leave respondWith() unsettled forever."""
        block = self._static_branch()
        non_exec = _extract_between(block, "// Non-executable static assets", "})\n  );\n});")
        assert "fetchWithTimeout(event.request, STATIC_NETWORK_TIMEOUT_MS)" in non_exec, (
            "non-executable static assets must still use the bounded "
            "fetchWithTimeout — removing this risks an unsettled "
            "respondWith() for images/fonts again"
        )
        assert "new Response('Offline', { status: 503 })" in non_exec, (
            "non-executable static assets must keep the fake-503-on-miss "
            "fallback when the network genuinely fails"
        )

    def test_response_ok_checked_before_every_cache_put_in_static_branch(self):
        """Caching a non-ok response (including a 503) would poison the
        cache for every future visit. Every cache.put(...) call in the
        static-asset branch must be preceded by a response.ok check on the
        same statement/guard."""
        block = self._static_branch()
        # Every cache.put( occurrence must be inside an `if (response.ok)` guard
        # opened on the same or a preceding line within a small window.
        for m in re.finditer(r"cache\.put\(", block):
            window_before = block[max(0, m.start() - 120):m.start()]
            assert "response.ok" in window_before, (
                "found a cache.put(...) not preceded by a response.ok check "
                "in the static-asset branch — caching a non-ok response "
                "(e.g. a 503) would poison the cache for every future visit"
            )

    def test_executable_branch_is_decided_before_the_bounded_fetch(self):
        """The executable-asset check must run before any bounded fetch in
        the static handler, so a script or stylesheet can never fall through
        to the timeout path."""
        block = self._static_branch()
        assert block.index("if (isExecutableAsset)") < block.index("fetchWithTimeout("), (
            "the isExecutableAsset branch must come before the first "
            "fetchWithTimeout() call in the static-asset handler"
        )


# ---------------------------------------------------------------------------
# Fix 2 — main.js: guard cross-file bare references
# ---------------------------------------------------------------------------

class TestMainJsBareRefGuards:
    def test_kline_context_menu_guarded_with_typeof(self):
        js = _main_js_text()
        assert re.search(
            r"typeof\s+klineContextMenu\s*!==\s*'undefined'\s*&&\s*klineContextMenu\s*&&\s*"
            r"!klineContextMenu\.contains\(e\.target\)",
            js,
        ), (
            "the chart app's document click handler must guard "
            "klineContextMenu (defined in the lazily-loaded "
            "kline-drawings.js) with a typeof check before touching it — an "
            "unguarded bare reference throws a ReferenceError on every click "
            "if kline-drawings.js failed to load"
        )

    def test_freehand_active_guarded_with_typeof(self):
        js = _main_js_text()
        assert re.search(
            r"typeof\s+_freehandActive\s*!==\s*'undefined'\s*&&\s*_freehandActive",
            js,
        ), (
            "handleKlineKeydown must guard _freehandActive (defined in "
            "freehand.js) with a typeof check before touching it — matches "
            "the klineContextMenu fix"
        )

    def test_no_more_bare_reads_of_the_two_guarded_globals(self):
        """Every `if (klineContextMenu` / `if
        (_freehandActive` occurrence in main.js must be the guarded typeof
        form, not a naked one."""
        js = _main_js_text()
        for name in ("klineContextMenu", "_freehandActive"):
            for m in re.finditer(r"if\s*\(\s*" + re.escape(name) + r"\b", js):
                window_before = js[max(0, m.start() - 60):m.start()]
                assert "typeof" in window_before, (
                    "found `if (%s...)` at main.js offset %d with no "
                    "preceding typeof guard in the same statement — every "
                    "boolean-context read of this cross-file global must be "
                    "guarded" % (name, m.start())
                )

    def test_kline_context_menu_guard_sits_in_a_document_click_listener(self):
        """The guard only protects later statements if it is inside the
        document-level click handler: the nearest preceding
        document.addEventListener must be the 'click' one."""
        js = _main_js_text()
        idx = js.index("typeof klineContextMenu !== 'undefined'")
        listeners = list(re.finditer(r"document\.addEventListener\(\s*'(\w+)'", js[:idx]))
        assert listeners and listeners[-1].group(1) == "click", (
            "the klineContextMenu typeof guard must sit inside a "
            "document-level 'click' listener"
        )

    def test_freehand_active_guard_sits_in_the_keydown_handler(self):
        """The _freehandActive guard must live inside handleKlineKeydown,
        ahead of the undo/redo and shortcut handling that follows it."""
        js = _main_js_text()
        start = js.index("function handleKlineKeydown(")
        guard = js.index("typeof _freehandActive !== 'undefined'")
        next_fn = js.find("\nfunction ", start + 1)
        assert start < guard < (next_fn if next_fn != -1 else len(js)), (
            "the _freehandActive typeof guard must be inside handleKlineKeydown"
        )


# ---------------------------------------------------------------------------
# Fix 3 — templates/index.html: script-load failure telemetry
# ---------------------------------------------------------------------------

class TestIndexHtmlAssetFailureTelemetry:
    def test_capture_phase_error_listener_present(self):
        html = _index_html_text()
        assert re.search(
            r"window\.addEventListener\(\s*'error'\s*,\s*function\s*\(e\)\s*\{.*?\}\s*,\s*true\s*\)",
            html, re.DOTALL,
        ), (
            "templates/index.html must register a capture-phase "
            "window 'error' listener (third arg `true`) — resource load "
            "errors (script/link) do not bubble, only capture"
        )

    def test_listener_filters_to_script_and_link_elements(self):
        html = _index_html_text()
        assert "'SCRIPT'" in html and "'LINK'" in html, (
            "the error listener must check e.target.tagName for SCRIPT/LINK "
            "so it only fires for failed static assets, not arbitrary JS "
            "errors"
        )

    def test_listener_calls_carraclog_with_buffer_fallback(self):
        """Must always land in the ring buffer: either via window.carracLog()
        when it's already defined, or by writing window.__carracLog directly
        as a fallback (carracLog itself isn't assigned until near the end of
        main.js's own execution — after most of the assets this listener
        exists to catch)."""
        html = _index_html_text()
        assert "window.carracLog(" in html or "window.carracLog ===" in html
        assert "'assets'" in html and "'load-failed'" in html
        assert "window.__carracLog" in html, (
            "must fall back to writing window.__carracLog directly when "
            "window.carracLog isn't defined yet"
        )

    def test_listener_beacons_to_sentry_when_available(self):
        html = _index_html_text()
        assert re.search(
            r"typeof\s+Sentry\s*!==\s*'undefined'.*?Sentry\.captureMessage\(",
            html, re.DOTALL,
        ), (
            "must beacon to Sentry.captureMessage(...) guarded by a typeof "
            "check (Sentry may not have loaded yet), matching this "
            "template's own existing Sentry-init guard convention"
        )
        assert "'static asset failed to load: '" in html

    def test_listener_registered_before_first_resource_tag(self):
        """Must sit as early as possible in load order — before the first
        <link>/<script> resource tag in the document — so it catches
        failures of everything after it, per the task's explicit ask."""
        html = _index_html_text()
        listener_idx = html.index("window.addEventListener('error'")
        first_link_idx = html.index("<link rel=")
        first_script_src_idx = html.index("<script src=")
        assert listener_idx < first_link_idx, (
            "the error listener must be registered before the first <link> "
            "resource tag"
        )
        assert listener_idx < first_script_src_idx, (
            "the error listener must be registered before the first "
            "<script src=...> resource tag"
        )

    def test_listener_stays_dependency_free(self):
        """It must work as the first script on the page, before any
        deferred file defines helpers, so it may only use platform APIs and
        typeof-guarded globals."""
        html = _index_html_text()
        start = html.index("window.addEventListener('error'")
        end = html.index("</script>", start)
        body = html[start:end]
        assert "require(" not in body and "import " not in body
        assert "typeof window.carracLog === 'function'" in body
