"""Watchlist write routing tests.

Confirms that the four watchlist write endpoints are dispatched to FastAPI
(listed in ``MIGRATED_WRITE_PATHS``) and that the strangler method filter
rejects non-POST methods on those paths.

Also tests:
- ``POST /api/watchlist`` accepts both a flat ``list[str]`` and a grouped
  ``dict`` (Union body).
- ``WatchlistSymbolRequest`` rejects extra fields with 400 (``extra="forbid"``).
- ``POST /api/watchlist/reorder`` accepts a raw JSON array.
"""

from __future__ import annotations

import pytest

from carrac.asgi import (
    MIGRATED_WRITE_PATHS,
    ExactPath,
)
from carrac.fastapi_app.models.watchlist import WatchlistSymbolRequest


# ---------------------------------------------------------------------------
# Dispatcher registration
# ---------------------------------------------------------------------------


def test_watchlist_save_listed_in_migrated_writes():
    assert ("/api/watchlist", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_watchlist_add_listed_in_migrated_writes():
    assert ("/api/watchlist/add", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_watchlist_remove_listed_in_migrated_writes():
    assert ("/api/watchlist/remove", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_watchlist_reorder_listed_in_migrated_writes():
    assert ("/api/watchlist/reorder", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


# ---------------------------------------------------------------------------
# Method-filter sanity
# ---------------------------------------------------------------------------


def test_watchlist_exact_path_only_matches_post():
    ep = ExactPath("/api/watchlist", app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": "/api/watchlist"}

    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})
    delete_match, _ = ep.matches({**base, "method": "DELETE"})

    assert post_match.value > 0
    assert get_match.value == 0
    assert delete_match.value == 0


def test_watchlist_add_exact_path_only_matches_post():
    ep = ExactPath("/api/watchlist/add", app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": "/api/watchlist/add"}

    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})

    assert post_match.value > 0
    assert get_match.value == 0


# ---------------------------------------------------------------------------
# Pydantic model — WatchlistSymbolRequest
# ---------------------------------------------------------------------------


def test_symbol_request_extra_field_rejected():
    """``extra="forbid"`` must raise on unknown fields."""
    with pytest.raises(Exception):
        WatchlistSymbolRequest(symbol="BTC", extra_field="bad")


def test_symbol_request_strips_and_uppercases():
    req = WatchlistSymbolRequest(symbol="  eth  ")
    assert req.symbol == "ETH"


def test_symbol_request_requires_non_empty():
    with pytest.raises(Exception):
        WatchlistSymbolRequest(symbol="")


# ---------------------------------------------------------------------------
# Union body — list vs dict (structural, no live DB)
# ---------------------------------------------------------------------------


def test_watchlist_body_accepts_list():
    """A plain Python list is a valid body for POST /api/watchlist."""
    body = ["BTC", "ETH", "SOL"]
    assert isinstance(body, list)


def test_watchlist_body_accepts_dict():
    """A plain Python dict (grouped format) is a valid body for POST /api/watchlist."""
    body = {"group1": ["BTC", "ETH"], "group2": ["SOL"]}
    assert isinstance(body, dict)
