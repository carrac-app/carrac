"""No file in static/js/ may be committed minified.

A minified source file is unreviewable, gets hand-edited in mangled form, and
makes the readable source look lost; a few lines of tens of thousands of
characters of mangled JavaScript can end up owning a whole UI panel nobody
dares touch.

The mechanism that causes it is a deploy accident, not a policy: `make minify`
rewrites static/js/*.js IN PLACE and `restore-js` puts them back afterwards. If
anything interrupts a deploy between those two steps, the tree is left holding
minified sources, and the next `git commit -a` bakes them in. That hazard
can recur at any time.

So this is the alarm for it. It runs inside `make test`, which gates
deploy, so a tree with minified sources cannot reach prod.
"""
import os

import pytest

# Long lines are normal in these: bundled/vendored code, and the icon subset.
EXEMPT = {
    'lucide-icons.js',   # generated icon subset, one long data line
    'ta-lib.js',         # vendored library
}

# Minified JS runs thousands of chars per line; hand-written JS in this repo
# stays well under 500. 2000 is far enough above normal that only genuine
# minification trips it.
MAX_LINE = 2000


def _js_sources():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'js')
    for name in sorted(os.listdir(root)):
        if not name.endswith('.js'):
            continue
        if name.endswith('.min.js') or name in EXEMPT:
            continue
        yield name, os.path.join(root, name)


def _longest_line(path):
    longest = 0
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            longest = max(longest, len(line.rstrip('\n')))
    return longest


def test_static_js_sources_are_not_minified():
    """Every static/js source keeps readable line lengths."""
    offenders = []
    for name, path in _js_sources():
        longest = _longest_line(path)
        if longest > MAX_LINE:
            offenders.append((name, longest))

    assert not offenders, (
        'These static/js files look MINIFIED (longest line > %d chars):\n  %s\n\n'
        'A deploy was probably interrupted between `make minify` and `restore-js`, '
        'leaving the tree holding minified sources. Recover with `make restore-js`, '
        'or `git checkout -- static/js/<file>` if the backup is gone. Do NOT commit '
        'them — that costs four months later.'
        % (MAX_LINE, '\n  '.join('%s (%d chars)' % (n, c) for n, c in offenders))
    )


def test_the_guard_can_actually_see_a_minified_file(tmp_path):
    """The guard is only worth having if it fires. Feed it a minified line."""
    fake = tmp_path / 'fake-bundle.js'
    fake.write_text('function a(b){return b}' + ('/*' + 'x' * MAX_LINE + '*/'), encoding='utf-8')
    assert _longest_line(str(fake)) > MAX_LINE


def test_split_layout_is_readable_source():
    """The file this guard was written for, pinned by name so it cannot silently regress."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, 'static', 'js', 'split-layout.js')
    if not os.path.exists(path):
        pytest.skip('split-layout.js not present')
    longest = _longest_line(path)
    assert longest <= MAX_LINE, (
        'split-layout.js is minified again (longest line %d). It owns the right-hand '
        'panel; recovering it the first time took a full de-minification pass.' % longest
    )
