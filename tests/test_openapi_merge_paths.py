"""merge_all_paths: the @spec layer is skipped only outside an app context.

Every @spec-decorated route reaches the published API document through
build_auto_paths. A failure there must surface, not leave the document
silently reduced to the hand-written paths.
"""

import pytest
from flask import Flask

import carrac.openapi.spec_decorator as spec_decorator
from carrac.openapi.paths import merge_all_paths


def test_build_auto_paths_failure_inside_app_context_is_raised(monkeypatch):
    def broken(app, include_internal=False):
        raise RuntimeError('broken @spec metadata')

    monkeypatch.setattr(spec_decorator, 'build_auto_paths', broken)
    with Flask(__name__).app_context():
        with pytest.raises(RuntimeError, match='broken @spec metadata'):
            merge_all_paths()


def test_auto_paths_are_layered_on_top_inside_app_context(monkeypatch):
    auto = {'/api/only-auto': {'get': {'summary': 'auto'}}}
    monkeypatch.setattr(spec_decorator, 'build_auto_paths',
                        lambda app, include_internal=False: auto)
    with Flask(__name__).app_context():
        paths = merge_all_paths()
    assert paths['/api/only-auto'] == {'get': {'summary': 'auto'}}
    assert '/api/plays' in paths  # hand-written paths are still there


def test_outside_app_context_returns_hand_written_paths_only(monkeypatch):
    def must_not_run(app, include_internal=False):
        raise AssertionError('build_auto_paths needs an app context')

    monkeypatch.setattr(spec_decorator, 'build_auto_paths', must_not_run)
    paths = merge_all_paths()
    assert '/api/plays' in paths
    assert '/api/only-auto' not in paths


def test_spec_built_outside_an_app_context_is_not_cached(app):
    from carrac.openapi import build_spec, reset_spec_cache

    reset_spec_cache()
    try:
        partial = build_spec()
        with app.app_context():
            full = build_spec()
        assert full is not partial
        assert len(full['paths']) >= len(partial['paths'])
    finally:
        reset_spec_cache()
