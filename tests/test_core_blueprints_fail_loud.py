"""Core blueprints must not be optional.

Eight in-repo core surfaces (charts, reports, brief archive, billing,
journal, scanner, legal, drawings/history) were registered inside
`except ImportError: logger.warning(...)`. If one import broke in production,
the whole route surface 404'd with a single boot-time log line nobody
reads. The
smoke suite would even bless it: /journal asserts only "no 5xx", and a
404 passes.

The catches dated from when these were optional add-ons. They are the
product now, and the file's own idiom for equally-core surfaces (AI,
portfolio, widget, openapi) is a bare import that refuses to boot.
Promote's rolling deploy makes that safe: the standby must pass /health
on the new image before traffic moves, so a broken build never serves.

Deliberately still allowed to degrade: flask-session (real optional
dependency) and websocket-client (documented REST-only fallback).
"""
import inspect

CORE_BLUEPRINTS = {
    'charts', 'reports', 'brief_archive', 'billing',
    'journal', 'scanner', 'legal', 'drawings', 'history',
}


def test_all_core_blueprints_are_registered(app):
    missing = CORE_BLUEPRINTS - set(app.blueprints)
    assert not missing, (
        f'core blueprints absent from the app: {sorted(missing)} — '
        f'their whole route surface would 404'
    )


def test_register_blueprints_does_not_swallow_an_import_failure():
    """A re-added `except ImportError` around a core registration turns a
    broken build into a silently-404ing prod. The two legitimate optional
    imports (flask-session, websocket-client) live in other functions and
    are asserted separately below."""
    from carrac import _register_blueprints
    src = inspect.getsource(_register_blueprints)
    assert 'except ImportError' not in src, (
        'a blueprint registration swallows ImportError again — a missing '
        'core module must refuse to boot, not 404 its surface'
    )


def test_the_two_legitimate_optional_imports_survive():
    import carrac
    src = inspect.getsource(carrac)
    assert 'flask-session not installed' in src
    assert 'websocket-client not installed' in src
