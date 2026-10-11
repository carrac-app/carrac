// The capture-phase asset-load-failure listener in templates/index.html must
// report failures that happen before Sentry.init().
//
// Example: on a first load, a block of sibling <script> tags (split-layout,
// plays-panel, share-modal, ...) can be lost client-side with no server-side
// trace; the only downstream symptom is `initSplitLayout is not defined`.
//
// The listener calls Sentry.captureMessage() the instant a
// resource's error event fires. That happens during PARSING, well before
// DOMContentLoaded — but Sentry.init() only runs inside the DOMContentLoaded
// handler further down the same template. Per the vendored SDK
// (static/js/vendor/sentry.bundle.min.js), Scope.captureMessage reads:
//   captureMessage(t,n,e){...if(!this.U)return c.warn("No client configured
//   on scope - will not capture message!"),r; ...}
// — `this.U` is the client attached by init(); with no client yet this is a
// silent no-op (and even that warn is invisible in production: terser strips
// console.* per the Makefile minify step). So every pre-init captureMessage
// call — exactly the "whole block of siblings never requested" case, which is
// the most worth knowing about — vanished with no trace.
//
// So: queue the src when Sentry isn't ready yet; flush the queue as ONE
// captureMessage (tags kind:asset_load_failed, count:N) right after
// Sentry.init() runs. Post-init failures (lazy loads) keep the existing
// per-src behaviour.
//
// This test slices the REAL two <script> blocks out of templates/index.html
// (not a reimplementation) and drives them in a vm sandbox against a FAKE
// Sentry client that reproduces the real bundle's pre-init no-op exactly.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = path.join(__dirname, '..', '..');
const HTML = fs.readFileSync(path.join(ROOT, 'templates/index.html'), 'utf8');

function extractScriptByMarker(html, marker) {
    // Requires an attribute (a space after "script") so a bare "<script>"
    // appearing as plain text inside an HTML comment — e.g. the capture-phase
    // comment right above this listener literally contains "<link>/<script>"
    // — is never mistaken for a real opening tag.
    const re = /<script\s[^>]*>/g;
    let m;
    while ((m = re.exec(html))) {
        const openEnd = m.index + m[0].length;
        const closeIdx = html.indexOf('</script>', openEnd);
        const body = html.slice(openEnd, closeIdx);
        if (body.includes(marker)) return body;
    }
    assert.fail('script containing "' + marker + '" not found in templates/index.html');
}

// Neutralise Jinja so the extracted block is plain JS: drop {% ... %} control
// tags (keeping their body — equivalent to the always-true/unauthenticated
// branches for this test's purposes) and stub {{ ... }} interpolations.
function jinjaToJs(src) {
    return src.replace(/\{%[^%]*%\}/g, '').replace(/\{\{[^}]*\|\s*tojson\s*\}\}/g, '"TEST"').replace(/\{\{[^}]*\}\}/g, 'TEST');
}

const LISTENER_SRC = jinjaToJs(extractScriptByMarker(HTML, "window.addEventListener('error'"));
// NOTE: marker must not be a substring that also appears inside the listener
// block's own comments (e.g. "Sentry.init(" — the listener's
// comments literally say "Sentry.init() has run") or extraction silently
// grabs the wrong <script> block.
const INIT_SRC = jinjaToJs(extractScriptByMarker(HTML, "document.addEventListener('DOMContentLoaded'"));

function harness() {
    const errorHandlers = [];
    const domContentLoadedHandlers = [];
    const sentryCalls = [];
    let sentryClientAttached = false;

    // Reproduces the vendored SDK's actual pre-init behaviour (verified by
    // reading static/js/vendor/sentry.bundle.min.js): captureMessage before
    // init() has attached a client to the scope is a silent no-op.
    const FakeSentry = {
        init() { sentryClientAttached = true; },
        setUser() {},
        captureMessage(msg, opts) {
            if (!sentryClientAttached) return;
            sentryCalls.push({ msg, opts });
        }
    };

    const sandbox = {
        console: { warn() {} },
        Sentry: FakeSentry,
        document: {
            addEventListener(type, cb) { if (type === 'DOMContentLoaded') domContentLoadedHandlers.push(cb); }
        },
        addEventListener(type, cb) { if (type === 'error') errorHandlers.push(cb); }
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);

    vm.runInContext(LISTENER_SRC, sandbox, { filename: 'index-error-listener.js' });
    vm.runInContext(INIT_SRC, sandbox, { filename: 'index-sentry-init.js' });

    return {
        fireAssetError(src) { errorHandlers.forEach((fn) => fn({ target: { tagName: 'SCRIPT', src } })); },
        fireDOMContentLoaded() { domContentLoadedHandlers.forEach((fn) => fn()); },
        sentryCalls
    };
}

test('a block of pre-init asset failures reaches Sentry as ONE event, after init, naming every src', () => {
    const h = harness();

    // The reported shape: 8 defer'd sibling scripts fail during PARSING,
    // i.e. before DOMContentLoaded (=before Sentry.init() runs).
    const failed = [
        '/static/js/split-layout.js', '/static/js/screening-matrix.js',
        '/static/js/plays-panel.js', '/static/js/plays-creation-form.js',
        '/static/js/plays-backtest-panel.js', '/static/js/share-modal.js',
        '/static/js/news-panel.js', '/static/js/multi-chart.js'
    ];
    failed.forEach((src) => h.fireAssetError(src));

    // No client attached yet — nothing should reach Sentry before init runs.
    assert.strictEqual(h.sentryCalls.length, 0);

    h.fireDOMContentLoaded(); // this is where Sentry.init() runs

    assert.strictEqual(h.sentryCalls.length, 1, 'the whole pre-init block must surface as exactly one event, not zero');
    const call = h.sentryCalls[0];
    failed.forEach((src) => assert.ok(call.opts.extra.srcs.includes(src), `event extra must name ${src}`));
    assert.strictEqual(call.msg, 'static assets failed to load before Sentry init', 'title must be constant so Bugsink groups one issue');
    assert.strictEqual(call.opts.tags.kind, 'asset_load_failed');
    assert.strictEqual(call.opts.tags.count, 8);
});

test('a post-init asset failure (e.g. a lazy load) still reports per-src, unbatched', () => {
    const h = harness();
    h.fireDOMContentLoaded(); // Sentry.init() has run, client attached
    h.fireAssetError('/static/js/kline-drawings.js');
    assert.strictEqual(h.sentryCalls.length, 1);
    assert.ok(h.sentryCalls[0].msg.includes('kline-drawings.js'));
    assert.strictEqual(h.sentryCalls[0].msg, 'static asset failed to load: /static/js/kline-drawings.js');
});

test('no pre-init failures: DOMContentLoaded does not emit an empty batch event', () => {
    const h = harness();
    h.fireDOMContentLoaded();
    assert.strictEqual(h.sentryCalls.length, 0);
});

test('a second later batch of pre-init failures (multiple queued) still becomes one event', () => {
    const h = harness();
    h.fireAssetError('/static/js/a.js');
    h.fireAssetError('/static/js/b.js');
    h.fireDOMContentLoaded();
    assert.strictEqual(h.sentryCalls.length, 1);
    assert.strictEqual(h.sentryCalls[0].opts.tags.count, 2);
});
