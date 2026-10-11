#!/usr/bin/env python3
"""Browser smoke gate: drive real Chromium through the app and fail on any JS error.

Route tests assert markup and the node tests in tests/js/ cover pure functions;
neither runs the app in a browser, so a page that renders its rows while a script
throws behind them passes both. This gate loads the pages for real.

The generalisable half of this file is not any single assertion below. It is
`page.on("pageerror")`. An uncaught exception anywhere in the page fails the run
and names the file and line. That is what closes the class; the click-paths just
make sure the interesting code is reached.

Needs Playwright with Chromium (the app image ships both; see the Dockerfile).

Usage against a running instance (the target is BROWSER_SMOKE_BASE, default
http://localhost:5000, which is the port inside the app container):

    # inside the app container, e.g. the one started by `make dev`
    docker exec -i -e SMOKE_LOGIN_EMAIL -e SMOKE_LOGIN_PASSWORD <app-container> \\
        python - < scripts/browser-smoke.py

    # or from any machine that has Playwright, against a local dev stack
    # (scripts/quickstart.sh publishes it on http://127.0.0.1:5002) or a
    # deployed instance
    BROWSER_SMOKE_BASE=http://localhost:5002 \\
    SMOKE_LOGIN_EMAIL=... SMOKE_LOGIN_PASSWORD=... python scripts/browser-smoke.py

Credentials come from environment variables only. SMOKE_LOGIN_EMAIL /
SMOKE_LOGIN_PASSWORD (an ordinary account) are required; without them the script
exits 1 rather than passing, because a run that never logged in proves nothing.
SMOKE_ADMIN_EMAIL / SMOKE_ADMIN_PASSWORD (an account whose email is the app's
ADMIN_EMAIL) are optional and enable the admin and premium page checks.
"""
import os
import sys


if __name__ == "__main__":
    # Answer --help before anything below is imported or touched.
    import argparse
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()

BASE = os.environ.get("BROWSER_SMOKE_BASE", "http://localhost:5000")
EMAIL = os.environ.get("SMOKE_LOGIN_EMAIL", "")
PASSWORD = os.environ.get("SMOKE_LOGIN_PASSWORD", "")
NAV_TIMEOUT = 20_000
ACT_TIMEOUT = 10_000

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

PASS = 0
FAIL = 0
page_errors = []
# Script/stylesheet requests that came back 4xx/5xx. Cleared per page in the
# 6c sweep so a failure is attributable to the page that caused it.
failed_assets = []
# API calls (fetch/XHR) that came back 4xx/5xx. NOT a failure on their own —
# some are expected — but when a page draws nothing this is almost always the
# reason, and without it the gate can only say "drew nothing" and leave you
# guessing.
failed_api = []


def ok(label):
    global PASS
    PASS += 1
    print(f"  {GREEN}✓{RESET} {label}")


def bad(label, detail=""):
    global FAIL
    FAIL += 1
    print(f"  {RED}✗{RESET} {label}")
    if detail:
        print(f"      {detail}")


def warn(label):
    print(f"  {YELLOW}⚠{RESET} {label}")


def main():
    if not EMAIL or not PASSWORD:
        print(f"{RED}✗ SMOKE_LOGIN_EMAIL / SMOKE_LOGIN_PASSWORD missing.{RESET}")
        print("  The browser gate cannot log in, so this run would prove nothing.")
        return 1

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_default_timeout(ACT_TIMEOUT)

        # THE class-closer. Anything the page throws and does not catch lands here.
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # The sibling of the class-closer above. One past defect was a script the
        # template never referenced; this catches the other half of the family
        # — a script it DOES reference that is no longer there.
        def _on_response(r):
            try:
                if r.status >= 400 and r.request.resource_type in ("script", "stylesheet"):
                    failed_assets.append(f"{r.status} {r.url}")
                elif r.status >= 400 and r.request.resource_type in ("fetch", "xhr"):
                    failed_api.append(f"{r.status} {r.url}")
            except Exception:
                pass  # a torn-down request during navigation is not a finding
        page.on("response", _on_response)

        print("\n  Browser smoke — real Chromium, real JS:")

        # ---- 1. log in through the actual form -------------------------------
        try:
            page.goto(f"{BASE}/login", timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            page.fill("input#email", EMAIL)
            page.fill("input#password", PASSWORD)
            page.click("button#submit-btn")
            page.wait_for_url(lambda u: "/login" not in u, timeout=NAV_TIMEOUT)
            ok(f"login through the form (landed on {page.url.replace(BASE, '') or '/'})")
            # Where login lands is a product decision that has changed before —
            # on some deployments it is /desk, not /. Navigate explicitly so this gate
            # tests the asset table rather than wherever the redirect points.
            page.goto(f"{BASE}/", timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
        except PWTimeout as e:
            bad("login through the form", f"still on /login after {NAV_TIMEOUT}ms: {e}")
            browser.close()
            return 1

        # ---- 2. the asset table actually renders rows ------------------------
        # The default tab is Watchlist, and the smoke account's watchlist is
        # empty — its only row is the "your watchlist is empty" placeholder,
        # which carries no data-symbol. Switch to All first, or this gate fails
        # on its own setup rather than on the app.
        try:
            page.wait_for_selector("#panel-tabs button.panel-tab", timeout=NAV_TIMEOUT)
            page.click('#panel-tabs button.panel-tab[data-tab="screener"]')
            page.wait_for_selector("#table-body tr[data-symbol]", timeout=NAV_TIMEOUT)
            rows = page.locator("#table-body tr[data-symbol]").count()
            ok(f"asset table rendered ({rows} rows)")

            # Slice net: the whole table engine (sort/filter/render/
            # load) lives in screener-table.js. The render half is
            # covered by the rows assertion above; this covers the BEHAVIOUR —
            # a real click on a sortable header must reorder the rows, and the
            # second click must reverse them. Fails if screener-table.js is
            # dropped from a template or its handleSort wiring dies.
            try:
                def _first_syms():
                    return page.evaluate(
                        "() => [...document.querySelectorAll('#table-body tr[data-symbol]')]"
                        ".slice(0, 8).map(r => r.getAttribute('data-symbol'))")
                page.click('th.sortable[data-sort="symbol"]')
                page.wait_for_timeout(400)
                asc = _first_syms()
                page.click('th.sortable[data-sort="symbol"]')
                page.wait_for_timeout(400)
                desc = _first_syms()
                if asc and desc and asc != desc:
                    ok(f"table sorts on header click ({', '.join(asc[:3])} -> {', '.join(desc[:3])})")
                else:
                    bad("table sorts on header click",
                        f"row order did not change across two header clicks "
                        f"(first row {asc[:1]} both times) — handleSort/"
                        f"renderFilteredTable (screener-table.js) "
                        f"is loaded but dead, or never loaded.")
            except Exception as e:
                bad("table sorts on header click", str(e)[:160])
        except PWTimeout:
            bad("asset table rendered", "no tr[data-symbol] appeared — loadData never completed")
            browser.close()
            return 1

        # ---- 3. the details drawer opens on a row click ----------------------
        # Flag-gated (the details-drawer feature flag in static/js/). If the flag is off we must SKIP loudly, not
        # score a pass — a green tick for an un-run check is how gates rot.
        drawer_on = page.evaluate(
            "() => !!(window._features && window._features.detailsDrawer && window.CarracDetails)"
        )
        if not drawer_on:
            warn("details drawer flag OFF here — drawer checks SKIPPED (not passed); "
                 "set DETAILS_DRAWER_ENABLED=1 in the app's .env and recreate the containers to run them")
        else:
            # Click a DRAWER-SAFE cell, not td.symbol. The row-click
            # handler (the drawer row-click handler in static/js/details-drawer.js) deliberately ignores two things:
            #
            #     [data-action="chart-preview"]     the logo + ticker span
            #     [data-action="toggle-watchlist"]  the star
            #
            # and between them they cover the whole of td.symbol — its centre
            # point, which is where Playwright clicks, resolves to SPAN.star.
            # So this check could never open the drawer; it toggled the smoke
            # account's watchlist instead. Click the price cell (below) rather
            # than the symbol cell.
            # td.price carries no data-action and is visible at 1440px.
            drawer_opened = False
            try:
                page.locator("#table-body tr[data-symbol]").first.locator(
                    "td.price"
                ).click()
                page.wait_for_selector("#cd-drawer.open", timeout=ACT_TIMEOUT)
                # #drStar is rendered by renderDrawer(), which only runs once
                # /api/asset/<sym>/detail resolves — it is NOT in the skeleton.
                # Wait for it, or checks 4 and 5 race the fetch.
                page.wait_for_selector("#drStar", timeout=ACT_TIMEOUT)
                drawer_opened = True
                ok("details drawer opens on a row click")
            except PWTimeout:
                bad("details drawer opens on a row click",
                    "#cd-drawer never opened — the dashboard drawer is dead")

            # ---- 4. THE STAR-WITHOUT-SYMBOL PATH --------------------------------------
            # With the drawer's #drStar now in the DOM, force the table to
            # re-render. A throw inside updateStarIcons here would silently
            # kill the rest of the render.
            # Close the drawer BEFORE reaching for the tab. #cd-scrim.show
            # is inset:0 / z-index:40 / pointer-events:auto, so while the drawer is
            # open elementFromPoint over any panel tab returns the scrim and the
            # click times out as "no inactive category tab". Closing costs nothing
            # here — #drStar STAYS in the DOM afterwards, which is exactly
            # the precondition for that defect (a .star with no data-symbol left behind).
            before = len(page_errors)
            try:
                page.keyboard.press("Escape")
                page.wait_for_selector("#cd-drawer:not(.open)", timeout=ACT_TIMEOUT)
                if not page.evaluate("() => !!document.getElementById('drStar')"):
                    warn("#drStar gone after close — star-without-symbol path not reproduced")
                page.click('#panel-tabs button.panel-tab[data-tab="crypto"]')
                page.wait_for_timeout(1200)
                if len(page_errors) > before:
                    bad("table re-renders after a drawer open (star-without-symbol path)",
                        page_errors[before])
                else:
                    ok("table re-renders after a drawer open (star-without-symbol path)")
            except PWTimeout:
                bad("table re-renders after a drawer open", "no inactive category tab to click")

            # ---- 5. the drawer's own star responds ---------------------------
            # It toggled the watchlist but never lit up, because the throw landed
            # between the two steps. Only a real click can see this.
            # Assert the star FLIPS and that the glyph agrees with
            # isInWatchlist() — do not assume it starts off. Requiring it to
            # end up lit would fail whenever the symbol was already on the
            # watchlist, which is a state this gate itself creates.
            # The defect's symptom is the glyph disagreeing with the state, and
            # that is what is checked here.
            if drawer_opened:
                try:
                    row = page.locator("#table-body tr[data-symbol]").first
                    sym = row.get_attribute("data-symbol")
                    row.locator("td.price").click()
                    page.wait_for_selector("#cd-drawer.open", timeout=ACT_TIMEOUT)
                    page.wait_for_selector("#drStar", timeout=ACT_TIMEOUT)
                except PWTimeout:
                    sym = None
                    warn("could not reopen the drawer — star check SKIPPED (not passed)")
                else:
                    star = page.locator("#drStar")
                    before = len(page_errors)
                    was_on = star.evaluate("el => el.classList.contains('on')")
                    star.click()
                    page.wait_for_timeout(900)
                    now_on = star.evaluate("el => el.classList.contains('on')")
                    in_wl = page.evaluate(
                        "s => typeof isInWatchlist === 'function' ? !!isInWatchlist(s) : null", sym
                    )
                    if len(page_errors) > before:
                        bad("drawer star click is clean", page_errors[before])
                    elif now_on == was_on:
                        bad("drawer star tracks the watchlist when clicked",
                            f"glyph never changed (stayed {'on' if was_on else 'off'}) "
                            "— the second symptom of the star-without-symbol defect")
                    elif in_wl is not None and in_wl != now_on:
                        bad("drawer star tracks the watchlist when clicked",
                            f"glyph says {'on' if now_on else 'off'} but isInWatchlist({sym}) "
                            f"says {in_wl} — the glyph and the state disagree")
                    else:
                        ok("drawer star tracks the watchlist when clicked")
                    # Put the watchlist back: a gate that mutates saved state
                    # changes what the NEXT run sees. That drift is what made
                    # this check non-deterministic in the first place.
                    star.click()
                    page.wait_for_timeout(600)
            else:
                warn("drawer never opened — star check SKIPPED (not passed)")

        # ---- 5c. the plays panel's extracted modules ------------
        # plays-panel.js was split into plays-creation-form.js and
        # plays-backtest-panel.js. Unlike the journal split, NOTHING here runs
        # at page load — every reference sits inside a function — so the page
        # renders perfectly with either file missing and every check above
        # stays green. That is the slice-1 trap, so drive the code instead.
        #
        # A click on #plays-new-btn will not do it: _isPlaysLocked() intercepts
        # for the free tier this gate logs in as, and shows the upgrade prompt
        # instead of the form. So call the entry point directly and assert the
        # DOM it is supposed to build.
        try:
            page.goto(f"{BASE}/", timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            res = page.evaluate(
                "() => {"
                "  if (!document.getElementById('plays-content'))"
                "    return { err: 'no #plays-content in the dashboard DOM' };"
                "  if (typeof _showCreationForm !== 'function')"
                "    return { err: '_showCreationForm missing —"
                " plays-creation-form.js did not load' };"
                "  if (typeof _canBacktestPlay !== 'function')"
                "    return { err: '_canBacktestPlay missing —"
                " plays-backtest-panel.js did not load' };"
                "  _showCreationForm();"
                "  return {"
                "    form: !!document.querySelector('#plays-content .plays-creation-form'),"
                "    pills: document.querySelectorAll("
                "             '#plays-type-pills button[data-value]').length,"
                "    bt: _canBacktestPlay({ primary_asset: null })"
                "  };"
                "}"
            )
            if res.get("err"):
                bad("plays creation form builds its step-1 DOM", res["err"])
            elif res.get("form") and res.get("pills") == 3 and res.get("bt") is False:
                ok("plays creation form builds its step-1 DOM "
                   f"({res['pills']} type pills, backtest gate live)")
            else:
                bad("plays creation form builds its step-1 DOM",
                    f"form={res.get('form')} pills={res.get('pills')} "
                    f"(expected 3) backtest={res.get('bt')!r} (expected False) — "
                    f"the extracted plays modules loaded but their code did not "
                    f"produce what it should.")
        except Exception as e:
            bad("plays creation form builds its step-1 DOM", str(e)[:160])

        # ---- 6. /desk, the route that prompted this check -------------------
        try:
            resp = page.goto(f"{BASE}/desk", timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            if resp and resp.status == 404:
                warn("/desk returns 404 (DESK_ENABLED off here) — desk checks SKIPPED")
            else:
                page.wait_for_timeout(1500)
                inited = page.evaluate(
                    "() => !!(window.CarracDetails && document.getElementById('cd-drawer'))"
                )
                if inited:
                    ok("/desk loads with the drawer initialized")
                else:
                    bad("/desk loads with the drawer initialized",
                        "CarracDetails.init() never ran on desk.html — the exact Critical "
                        "nine reviews missed")
        except PWTimeout:
            bad("/desk loads", f"navigation timed out after {NAV_TIMEOUT}ms")

        # ---- 6b. /chart/<symbol>/<tf>, the standalone chart page --------------
        # This page can be completely dead while nothing notices:
        # chart_standalone.html loads main.js, so if indicator-registry.js is
        # missing here, main.js throws at MODULE SCOPE and every line after
        # it — the entire chart — never runs. The page renders "Loading" forever.
        #
        # Assert the chart actually DREW, not that the route answered 200. A
        # 200 is exactly what a dead page still returns.
        try:
            # POLL, do not sleep. A fixed wait (9s then 13s) false-reds the first
            # post-deploy smoke run (`bars=-1 canvases=0 after TWO attempts`) on
            # a page that is completely healthy a minute later. The cause is the
            # warm-up window below, but polling is right regardless — a cold
            # first load CAN outlast any sleep you are willing to hard-code.
            #
            # A gate that is red exactly when it runs, on the deploy path, for a
            # reason nobody can act on is the worst possible shape; it is how a
            # drift guard becomes noise. Polling fixes both
            # ends: a warm chart passes in a second or two instead of nine, and
            # a cold one gets a generous deadline before anyone is told.
            #
            # It does NOT weaken the assertion. A page whose script is missing
            # never draws, so it still fails — it just fails after 40s of being
            # given every chance, which is what makes the red trustworthy.
            CHART_DEADLINE_MS = 40_000
            POLL_MS = 1_000
            state = {"bars": -1, "canvases": 0}
            # The fixture symbol MUST be curated (database.py crypto_map). A
            # NON-curated symbol (e.g. BTC-USD) routes every
            # request through a live Yahoo validation call before any cache
            # (the chart route's symbol validation). For ~140s after every restart the app's
            # startup fetches + the worker's HTF warmup exhaust the shared
            # yahoo_minute Redis budget (10/min), so the gate 404-blanks on
            # exactly the deploy path, most runs, while the same URL is
            # healthy minutes later. That gates deploys on Yahoo's rate budget,
            # not on our code. Curated BTC reads the host-persistent CSV cache
            # and never touches Yahoo. The dynamic-tier fragility is a known issue;
            # so is the page blanking on a data 404.
            page.goto(f"{BASE}/chart/BTC/1D", timeout=NAV_TIMEOUT,
                      wait_until="domcontentloaded")
            waited = 0
            while waited < CHART_DEADLINE_MS:
                state = page.evaluate(
                    "() => ({"
                    "  bars: (typeof klineChart !== 'undefined' && klineChart"
                    "         && klineChart.getDataList) ? klineChart.getDataList().length : -1,"
                    "  canvases: document.querySelectorAll('canvas').length"
                    "})"
                )
                if state["bars"] > 0 and state["canvases"] > 0:
                    break
                page.wait_for_timeout(POLL_MS)
                waited += POLL_MS
            if state["bars"] > 0 and state["canvases"] > 0:
                ok(f"/chart/<symbol>/<tf> renders a real chart "
                   f"({state['bars']} bars, {state['canvases']} canvases)")

                # The chart RENDERING and the chart RESPONDING are
                # two different claims, and breaking up main.js only threatens
                # the second one. The gesture handlers moved to
                # chart-gestures.js are pure declarations — a page can draw all
                # 14 canvases with every one of them missing, and every check
                # above would still be green.
                #
                # So drive a real key and assert the chart MOVED. This is the
                # net under the rest of the extraction: it fails if an extracted file is
                # not loaded, is loaded in the wrong order, or lost a symbol on
                # the way out.
                try:
                    before = page.evaluate(
                        "() => { const r = klineChart.getVisibleRange();"
                        "        return r.from + ':' + r.to; }")
                    page.locator("body").click(position={"x": 700, "y": 400})
                    for _ in range(12):
                        page.keyboard.down("ArrowLeft")
                        page.wait_for_timeout(60)
                        page.keyboard.up("ArrowLeft")
                    page.wait_for_timeout(1200)
                    after = page.evaluate(
                        "() => { const r = klineChart.getVisibleRange();"
                        "        return r.from + ':' + r.to; }")
                    if before != after:
                        ok(f"chart responds to arrow-key navigation ({before} -> {after})")
                    else:
                        bad("chart responds to arrow-key navigation",
                            f"visible range stayed {before} after 12 ArrowLeft presses — "
                            f"the chart drew but does not respond. Most likely "
                            f"chart-gestures.js did not load.")
                except Exception as e:
                    bad("chart responds to arrow-key navigation", str(e)[:160])

                # Slice net: toolbar-tooltips.js is separate from
                # main.js. Its behaviour, not its declarations —
                # a real hover over a toolbar button must produce a visible
                # #cx-tooltip. Fails if the file is dropped from a template,
                # loads in a broken order, or its listeners never registered.
                try:
                    page.hover(".adv-chart-toolbar button >> nth=0")
                    tip = None
                    for _ in range(20):
                        tip = page.evaluate(
                            "() => { const t = document.getElementById('cx-tooltip');"
                            "        return t && t.style.display === 'block'"
                            "               ? t.textContent.trim().slice(0, 40) : null; }")
                        if tip:
                            break
                        page.wait_for_timeout(100)
                    if tip:
                        ok(f"toolbar tooltip appears on hover ({tip!r})")
                    else:
                        bad("toolbar tooltip appears on hover",
                            "no visible #cx-tooltip within 2s of hovering a toolbar "
                            "button — toolbar-tooltips.js (extracted module) is "
                            "loaded but its behaviour is dead, or it never loaded.")
                except Exception as e:
                    bad("toolbar tooltip appears on hover", str(e)[:160])

                # Slice 8: every CUSTOM overlay registration (rect,
                # fib, measureTool, the alert family, milestonePin, anchored
                # VWAP, the custom glyphs …) moved to chart-overlay-defs.js.
                # "The chart rendered" does not cover it — klinecharts draws
                # candles fine with every custom overlay type missing. So
                # actually CREATE one: klinecharts returns null for a name it
                # never registered, so this goes red if the file did not load,
                # ran only partway, or lost a registration on the way out.
                try:
                    res = page.evaluate(
                        "() => {"
                        "  const dl = klineChart.getDataList();"
                        "  if (!dl || dl.length < 3) return { err: 'chart has no data' };"
                        "  const a = dl[dl.length - 3], b = dl[dl.length - 1];"
                        "  let id = null;"
                        "  try {"
                        "    id = klineChart.createOverlay({ name: 'measureTool',"
                        "      points: [{ timestamp: a.timestamp, value: a.close },"
                        "               { timestamp: b.timestamp, value: b.close }] });"
                        "  } catch (e) { return { err: String(e).slice(0, 120) }; }"
                        "  const found = id ? !!klineChart.getOverlayById(id) : false;"
                        "  if (id) { try { klineChart.removeOverlay(id); } catch (e) {} }"
                        "  return { flag: !!window._chartTypesRegistered, id: id || null,"
                        "           found: found };"
                        "}"
                    )
                    if res.get("err"):
                        bad("a custom overlay type can be created", res["err"])
                    elif res.get("id") and res.get("found") and res.get("flag"):
                        ok(f"a custom overlay type can be created "
                           f"(measureTool -> {res['id']})")
                    else:
                        bad("a custom overlay type can be created",
                            f"createOverlay('measureTool') gave id={res.get('id')!r} "
                            f"found={res.get('found')} _chartTypesRegistered="
                            f"{res.get('flag')} — the overlay registrations in "
                            f"chart-overlay-defs.js (slice 8) did not run.")
                except Exception as e:
                    bad("a custom overlay type can be created", str(e)[:160])
            else:
                detail = (f"bars={state['bars']} canvases={state['canvases']} after "
                          f"{CHART_DEADLINE_MS // 1000}s of polling — the page answered "
                          f"but drew nothing.")
                if failed_api:
                    detail += (f" {len(failed_api)} API call(s) failed during this run; "
                               f"last few: " + "; ".join(failed_api[-4:]))
                else:
                    detail += (" No API call failed, so suspect a missing script rather "
                               "than data.")
                bad("/chart/<symbol>/<tf> renders a real chart", detail)
        except PWTimeout:
            bad("/chart/<symbol>/<tf> renders a real chart",
                f"navigation timed out after {NAV_TIMEOUT}ms")

        # ---- 6c. EVERY other page renders too --------------------
        # A page can be blank while the server answers 200. Loading only a few
        # pages in a real browser leaves the rest of the template-rendering GET
        # routes free to be dead while every HTTP-level gate stays green —
        # those gates ask whether the SERVER ANSWERED, never whether the BROWSER
        # DREW ANYTHING. A 200 is exactly what a dead page returns.
        #
        # Three signals per page, because no single one is sufficient:
        #
        #   1. errors   — uncaught exceptions attributed to THIS page. This is
        #                 the blank-page detector: a script missing from the
        #                 template makes the bundle throw at module scope.
        #   2. assets   — script/stylesheet requests that 404. Catches a
        #                 renamed or unminified bundle that the template still
        #                 points at.
        #   3. proof    — for pages whose UI is BUILT BY JS, an expression that
        #                 is truthy only if that JS ran to completion. Each one
        #                 was checked against the template to confirm it is not
        #                 already present in the server-rendered markup — a
        #                 selector that matches static Jinja output proves
        #                 nothing, because a stone-dead page would still match.
        #
        # `floor` is a minimum visible-text length. It is the blunt backstop for
        # "the page went blank", which is what that defect looked like to a user:
        # the word "Loading" and nothing else. Set at roughly half of what each
        # page actually rendered, so ordinary copy edits do not
        # trip it.
        #
        # proof=None is NOT laziness — those pages are deliberately pure
        # server-rendered Jinja with no page JavaScript at all (asset_detail.html
        # and brief_archive.html contain zero <script> tags). There is no "did
        # the JS run" question to ask, so asking it would be theatre. They still
        # get signals 1, 2 and the floor.
        # The shared marketing nav swaps <i data-lucide> for a real <svg>. If
        # that bundle dies, every marketing page loses its chrome at once, so
        # one expression covers all of them.
        _LUCIDE = "!!document.querySelector('#toTop svg[data-lucide=\"arrow-up\"]')"

        PAGES = [
            # path,                      floor, proof (JS-built) or None
            ("/",                         5000, "!!window.klineChart"),
            # The KPI-bar count alone is a RENDER proof, and this
            # page's playbooks/goals + equity/analytics sections now live in
            # separate files. initGoals() runs at page load and fills
            # #j-goals-month with 12 options whose labels come from
            # MONTH_NAMES — which reaches the extracted module only through
            # the window.JournalCore alias. So the option count proves the
            # module loaded, and the label format proves the alias resolved
            # (a broken alias yields 0 options, not 12 blank ones).
            ("/journal",                   400,
             "document.querySelectorAll('#j-kpi-bar .j-kpi').length === 7"
             " && document.querySelectorAll('#j-goals-month option').length === 12"
             " && /^[A-Z][a-z]+ [0-9]{4}$/.test("
             "document.querySelector('#j-goals-month option').textContent)"),
            ("/portfolio",                 150,
             "document.getElementById('pf-book-value').textContent.trim() !== '—'"),
            ("/settings",                  200,
             "document.getElementById('acct-email').textContent.trim() !== '—'"),
            ("/report/pulse",              900,
             "document.querySelectorAll('.pulse-weather-loading, "
             ".pulse-narrative-loading').length === 0"),
            ("/guide",                   40000, "document.querySelector('.sb-link.active') !== null"),
            ("/api/docs",                 4000, "document.querySelector('#swagger-ui .swagger-ui') !== null"),
            # /brief gets no JS proof, and the reason is worth keeping. Its only
            # JS-built signal is the sentiment ribbon, and that is DATA-shaped:
            # on a fresh install `window.__briefData` is undefined and every ribbon cell
            # is an em dash, because a fresh install carries no quant data. Asserting it
            # would fail on missing data rather than on broken rendering — a
            # check that is red for a reason nobody can fix is one people learn
            # to skip. Floor + error + asset signals still apply.
            # /brief has two honest shapes. Reports running -> the
            # live editorial page (thousands of chars). Reports paused -> the
            # archive index with a <div data-brief-paused> notice, which on
            # a fresh install (no stored reports) is ~400 chars. The proof accepts either
            # shape and still fails on a blank page, because the floor stays.
            ("/brief",                     150,
             "document.querySelector('[data-brief-paused]') !== null || "
             "document.body.innerText.length >= 3000"),
            # Marketing chrome: the i[data-lucide] -> <svg> swap only happens if
            # the shared nav bundle executed. Page-specific JS does not exist on
            # these by design, so this is what there is to prove.
            # One URL, two templates: `carrac/dashboard/routes.py::intelligence_page` serves
            # intelligence_app.html to premium/tester/admin and the marketing
            # intelligence.html to everyone else. So the expectations have to
            # cover both, or the check reports on the tier of whoever happens to
            # be logged in. Caught by this gate going red the moment the smoke
            # account was temporarily made admin: 757 chars against a floor of
            # 3000, on a page that was rendering perfectly.
            ("/intelligence",              400,
             "(" + _LUCIDE + ") || (document.getElementById('tab-indicator') && "
             "document.getElementById('tab-indicator').style.width !== '')"),
            ("/changelog",              150000, _LUCIDE),
            ("/indicators",              15000, _LUCIDE),
            ("/formulas",                 9000, _LUCIDE),
            # Pure server-rendered Jinja — zero <script> tags. See note above.
            ("/assets",                    300, None),
            ("/assets/BTC",                300, None),
            ("/brief/archive",             150, None),
            ("/report/morning/archive",    150, None),
            ("/journal/guide",            4000, None),
            ("/contact",                   400, None),
            ("/privacy",                  7000, None),
            ("/terms",                    3700, None),
            ("/disclaimer",               2600, None),
            ("/markets",                  1500, None),
            ("/mcp",                      2600, None),
            ("/integrations",             1900, None),
            ("/workflow",                 2700, None),
            ("/market-regime",            3200, None),
        ]

        def sweep(pg, pages, label=""):
            """Run the three signals over `pages` on `pg`; return how many passed.

            Shared by the logged-in pass and the admin pass on purpose. A second
            hand-rolled copy is how two checks drift until one is quietly weaker
            than the other.
            """
            passed = 0
            for path, floor, proof in pages:
                # Two attempts, for the same reason the chart check above retries:
                # several of these proofs depend on a fetch resolving (the /brief
                # ribbon, the pulse narrative), and a single slow response would
                # otherwise paint a healthy page red. Observed for real while
                # building this — a deliberate break of /journal also reddened
                # /brief, which that break could not possibly have caused.
                #
                # A genuinely dead page fails BOTH attempts: a script the template
                # never loads does not appear on a reload. The cost of the retry is
                # paid only on pages that already look broken.
                state, errs, assets, landed, status = None, [], [], path, None
                for attempt in (1, 2):
                    before = len(page_errors)
                    del failed_assets[:]
                    try:
                        resp = pg.goto(f"{BASE}{path}", timeout=NAV_TIMEOUT,
                                         wait_until="domcontentloaded")
                        status = resp.status if resp else None
                        pg.wait_for_timeout(2500 if attempt == 1 else 6000)
                    except PWTimeout:
                        state = None
                        continue

                    # A redirect means a gate sent us elsewhere (flag off, wrong
                    # tier, not admin). SKIP loudly — scoring that as a pass is how
                    # a gate ends up green on pages it never actually loaded.
                    landed = pg.evaluate("() => location.pathname")
                    if landed != path:
                        break

                    state = pg.evaluate(
                        "(p) => ({"
                        "  text: (document.body ? (document.body.innerText || '') : '').trim().length,"
                        "  proof: p ? !!eval(p) : null"
                        "})", proof)
                    errs = page_errors[before:]
                    assets = list(failed_assets)
                    if not errs and not assets and state["text"] >= floor \
                            and (proof is None or state["proof"]) \
                            and not (status and status >= 400):
                        break

                if landed != path:
                    warn(f"{label}{path} -> {landed} (gated here) — SKIPPED, not passed")
                    continue
                if state is None:
                    bad(f"{label}{path} renders", f"navigation timed out after {NAV_TIMEOUT}ms, twice")
                    continue

                # A Flask error page has plenty of visible text and
                # no JS error, so the checks below would pass a page
                # (e.g. /report/morning/archive) that answers 500.
                if status and status >= 400:
                    bad(f"{label}{path} renders", f"HTTP {status} — the server answered "
                        f"with an error page, whatever it looks like")
                elif errs:
                    bad(f"{label}{path} renders without throwing", errs[0][:200])
                elif assets:
                    bad(f"{label}{path} loads all its scripts", f"{assets[0]} — the template points at "
                        f"something that is not there")
                elif state["text"] < floor:
                    bad(f"{label}{path} renders", f"only {state['text']} chars of visible text "
                        f"(floor {floor}) — the page came back blank or near-blank")
                elif proof is not None and not state["proof"]:
                    bad(f"{label}{path} renders", f"page loaded but its own JavaScript never built the "
                        f"UI: `{proof[:70]}` is falsy. This is the blank-page shape.")
                else:
                    passed += 1
            return passed

        swept = sweep(page, PAGES)
        ok(f"{swept}/{len(PAGES)} other pages render (real DOM, not a 200)")

        # ---- 6d. the admin + premium pages -----------------
        # These 12 templates are invisible to the pass above, and the reason is
        # worth stating: the sweep runs as an ordinary free-tier user, so every
        # /admin/* route answers 403 and /intelligence quietly serves the
        # MARKETING page instead of the app. They get SKIPPED, correctly — and
        # a skipped page is not a checked page. Twelve templates could be in
        # the blank-page state and nothing here would say so.
        #
        # Deliberately a SEPARATE credential rather than making the smoke
        # account admin. An admin session sees dark features and admin-gated
        # pages, so promoting the main account would stop the 31 checks above
        # from testing what a real user actually gets — it would trade the
        # coverage we have for the coverage we lack.
        #
        ADMIN_PAGES = [
            # Floors are low on purpose: a fresh install's admin tables are empty, so
            # these pages legitimately render little more than their nav and an
            # empty state. The signal here is "it rendered at all", not volume.
            ("/admin",            150, None),
            ("/admin/users",      150, None),
            ("/admin/usage",      150, None),
            ("/admin/costs",      200, None),
            ("/admin/tiers",      800, None),
            ("/admin/billing",    200, None),
            ("/admin/feedback",   120, None),
            ("/admin/health",     600, None),
            ("/admin/intents",    900, None),
            ("/admin/waitlist",   150, None),
            ("/design-system",   2500, None),
            # The intelligence APP, not the marketing page a free account gets.
            # #tab-indicator is empty in the template and sized by JS from
            # getBoundingClientRect, so a width proves the page's own script ran.
            ("/intelligence",     400,
             "document.getElementById('tab-indicator').style.width !== ''"),
        ]
        ADMIN_EMAIL = os.environ.get("SMOKE_ADMIN_EMAIL", "")
        ADMIN_PASSWORD = os.environ.get("SMOKE_ADMIN_PASSWORD", "")
        if not ADMIN_EMAIL or not ADMIN_PASSWORD:
            warn(f"SMOKE_ADMIN_EMAIL / SMOKE_ADMIN_PASSWORD unset — "
                 f"{len(ADMIN_PAGES)} admin/premium templates SKIPPED, not passed")
            print("      Export them in the environment (an account whose email is the "
                  "app's ADMIN_EMAIL; on a local\n      dev stack, sign up with that "
                  "email), or these templates stay unchecked by anything.")
        else:
            # A SEPARATE context, not just a second page. Pages in one context
            # share cookies, so the admin login landed on an already-signed-in
            # session and never left /login. Observed, not theorised.
            actx = browser.new_context(viewport={"width": 1440, "height": 900})
            apage = actx.new_page()
            apage.set_default_timeout(ACT_TIMEOUT)
            apage.on("pageerror", lambda e: page_errors.append(f"[admin] {e}"))
            apage.on("response", _on_response)
            try:
                apage.goto(f"{BASE}/login", timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                apage.fill("input#email", ADMIN_EMAIL)
                apage.fill("input#password", ADMIN_PASSWORD)
                apage.click("button#submit-btn")
                apage.wait_for_url(lambda u: "/login" not in u, timeout=NAV_TIMEOUT)
            except PWTimeout:
                bad("admin login through the form",
                    "still on /login — admin pages cannot be checked this run")
            else:
                # Confirm the session really is admin before scoring anything.
                # Without this, a non-admin credential would 403 every page, the
                # sweep would SKIP them all, and the run would still look fine —
                # a gate reporting success over twelve pages it never loaded.
                r = apage.goto(f"{BASE}/admin", timeout=NAV_TIMEOUT,
                               wait_until="domcontentloaded")
                if r is not None and r.status != 200:
                    bad("SMOKE_ADMIN_* account is actually an admin",
                        f"/admin returned {r.status} — the credential is set but not "
                        f"privileged, so these 12 templates are still unchecked")
                else:
                    a_swept = sweep(apage, ADMIN_PAGES, "admin ")
                    ok(f"{a_swept}/{len(ADMIN_PAGES)} admin + premium pages render")
            actx.close()

        browser.close()

    # ---- 7. anything the page threw anywhere in the run ----------------------
    if page_errors:
        bad(f"no uncaught page errors ({len(page_errors)} thrown)")
        for e in page_errors[:5]:
            print(f"      {e}")
    else:
        ok("no uncaught page errors during the whole run")

    print()
    if FAIL:
        print(f"{RED}  {FAIL} failed, {PASS} passed.{RESET}")
        return 1
    print(f"{GREEN}  All {PASS} browser checks passed.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
