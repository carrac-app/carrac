"""Last-known-good (LKG) scanner fallback.

The fresh scanner cache key has a 1h TTL; on a worker restart before the first
precompute pass — or TTL expiry without a refresh — it is empty and the UI
Scanner/Radar 503 / render blank for tens of minutes. ``set_cached_scanner``
now also persists an UNTIMED LKG copy, and ``get_cached_scanner`` serves it
STALE-MARKED, but ONLY when the caller opts in (``allow_stale=True``). Alert /
report / MCP evaluators keep the strict fresh-or-None contract (default), so
nothing evaluates against stale data.
"""

import pytest

from carrac import caches

_FAKE = {
    "results": [{"symbol": "BTC", "best_verdict": "STRONG"}],
    "macro": {"regime": "Risk-On"},
    "computed_at": "2026-07-09T10:00:00Z",
}


@pytest.fixture(autouse=True)
def _local_only(monkeypatch):
    """Force the in-memory path (no Redis) and hard-reset the process-global LKG
    so tests can't leak into each other (the conftest-reset gotcha)."""
    monkeypatch.setattr(caches, "_get_redis", lambda: None)
    caches._scanner._local.clear()
    caches._scanner_lkg_local = None
    yield
    caches._scanner._local.clear()
    caches._scanner_lkg_local = None


def _expire_fresh_key():
    """Simulate the fresh key being gone (TTL expiry / worker restart)."""
    caches._scanner._local.clear()


def test_default_returns_none_on_cold_start():
    # Strict contract for alert/report/MCP evaluators: NO stale leak.
    caches.set_cached_scanner(_FAKE)
    _expire_fresh_key()
    assert caches.get_cached_scanner() is None


def test_allow_stale_serves_lkg_marked_stale_on_cold_start():
    caches.set_cached_scanner(_FAKE)
    _expire_fresh_key()
    got = caches.get_cached_scanner(allow_stale=True)
    assert got is not None, "cold-start UI read must serve the last-known-good snapshot"
    assert got["_stale"] is True
    assert isinstance(got["_stale_age_sec"], int) and got["_stale_age_sec"] >= 0
    assert got["results"][0]["symbol"] == "BTC"
    assert got["computed_at"] == "2026-07-09T10:00:00Z"  # old timestamp = visibly stale


def test_allow_stale_returns_fresh_unmarked_when_present():
    caches.set_cached_scanner(_FAKE)
    got = caches.get_cached_scanner(allow_stale=True)
    assert got is not None
    assert "_stale" not in got, "fresh data must never be marked stale"


def test_allow_stale_none_when_never_stored():
    _expire_fresh_key()
    assert caches.get_cached_scanner(allow_stale=True) is None


def test_lkg_does_not_mutate_cached_object():
    caches.set_cached_scanner(_FAKE)
    _expire_fresh_key()
    caches.get_cached_scanner(allow_stale=True)
    # The stale marker must live on a shallow copy, never on the stored dict.
    assert "_stale" not in _FAKE
