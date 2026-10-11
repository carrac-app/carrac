"""Plays writes routing tests.

These tests exercise the strangler dispatcher to confirm that ``POST
/api/plays`` and the writes under ``/api/plays/<id>`` go to the FastAPI
runtime while the reads stay on Flask. Handler behaviour over HTTP is
covered by tests/fastapi/test_plays_api.py.
"""

from __future__ import annotations

from carrac.asgi import (
    MIGRATED_PATHS,
    MIGRATED_WRITE_PATHS,
    MIGRATED_WRITE_PREFIXES,
    ExactPath,
    PrefixPath,
)


def test_method_filtered_exact_path_only_matches_listed_methods():
    ep = ExactPath("/api/plays", app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": "/api/plays"}

    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})
    put_match, _ = ep.matches({**base, "method": "PUT"})

    assert post_match.value > 0
    assert get_match.value == 0
    assert put_match.value == 0


def test_method_filtered_prefix_path():
    pp = PrefixPath(
        "/api/plays/", app=None, methods=frozenset({"PUT", "DELETE"})
    )
    base = {"type": "http", "path": "/api/plays/123"}

    put_match, _ = pp.matches({**base, "method": "PUT"})
    del_match, _ = pp.matches({**base, "method": "DELETE"})
    get_match, _ = pp.matches({**base, "method": "GET"})

    assert put_match.value > 0
    assert del_match.value > 0
    assert get_match.value == 0


def test_no_method_filter_matches_all():
    """Without a methods filter, ExactPath matches every method."""
    ep = ExactPath("/health", app=None)
    base = {"type": "http", "path": "/health"}
    for method in ("GET", "POST", "PUT", "DELETE", "OPTIONS"):
        match, _ = ep.matches({**base, "method": method})
        assert match.value > 0, method


def test_active_plays_get_unaffected_by_write_prefix_filter():
    """``GET /api/plays/active`` (ExactPath) is matched BEFORE
    the method-filtered prefix in the route order, so the
    method filter on the prefix never applies. Sanity-check the data:
    """
    assert "/api/plays/active" in MIGRATED_PATHS
    # The prefix is method-filtered AND only catches /api/plays/<id>
    # writes. /api/plays/active (a sibling path) is matched by the
    # ExactPath above this in the dispatcher list — the prefix entry
    # never gets a chance to run for that path under GET because the
    # methods filter rejects GET.
    # Wave-2 extended the methods set with POST so /<id>/{link-drawing,
    # unlink-drawing, evaluate, backtest} subroutes reach FastAPI.
    assert (
        "/api/plays/", frozenset({"PUT", "DELETE", "POST"})
    ) in MIGRATED_WRITE_PREFIXES


def test_post_plays_listed_in_migrated_writes():
    assert ("/api/plays", frozenset({"POST"})) in MIGRATED_WRITE_PATHS
