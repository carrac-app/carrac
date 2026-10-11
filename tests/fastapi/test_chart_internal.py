"""FastAPI chart-internal endpoint tests.
The cache-aware tests were added in the second half of this file.

Covers:
* 401 when no API key supplied
* 401 when an invalid API key is supplied
* 200 with a valid admin API key (Binance fetch + indicator computation mocked)
* 404 when build_chart_payload raises ChartNotFoundError
* Service import smoke test (no Flask imports)
* Router import smoke test
* Fresh cache hit skips build; stale hit returns old payload +
  schedules exactly one background rebuild (no stampede under concurrency);
  cache miss builds + caches; the `:int` key variant never collides with a
  plain /api/chart key; `?fresh=1` bypasses the lookup but still writes
  back; `?days=` bypasses the cache entirely (read AND write).
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport


API_KEY = os.environ.get("SCREENSHOT_API_KEY", "test-api-key-for-testing")

# Minimal payload shape returned by the mocked service.
_MOCK_PAYLOAD = {
    "ohlcv": [{"time": 1700000000, "open": 30000.0, "high": 31000.0, "low": 29000.0, "close": 30500.0}],
    "volume": [],
    "rsi": [],
    "macd": [],
    "symbol": "BTC",
    "timeframe": "1D",
    "cpr": None,
    "weekly_cpr": None,
    "daily_cpr": None,
    "indicators": {},
    "indicators_ext": {},
}


@pytest.fixture(autouse=True)
def _reset_chart_internal_singleflight():
    """Clear chart_internal.py's own in-process single-flight guard between
    tests. Only touches state this router owns (not carrac.caches) — every
    cache-behavior test below mocks get_cached_chart/set_cached_chart
    directly, so the real cache is never touched, but a leftover in-flight
    marker from one test could suppress another test's background rebuild."""
    from carrac.fastapi_app.routers import chart_internal as _ci

    _ci._inflight.clear()
    _ci._background_tasks.clear()
    yield
    _ci._inflight.clear()
    _ci._background_tasks.clear()


# ---------------------------------------------------------------------------
# Service import smoke test
# ---------------------------------------------------------------------------


def test_service_imports_cleanly():
    """build_chart_payload is importable without Flask context."""
    from carrac.charts.service import build_chart_payload, ChartNotFoundError  # noqa: F401


# ---------------------------------------------------------------------------
# Router import smoke test
# ---------------------------------------------------------------------------


def test_chart_internal_router_imports_cleanly():
    """FastAPI chart_internal router is importable."""
    from carrac.fastapi_app.routers import chart_internal  # noqa: F401


# ---------------------------------------------------------------------------
# Auth gate tests
# ---------------------------------------------------------------------------


def test_chart_internal_no_key_returns_401(asgi_client):
    """GET /api/chart-internal/BTC without any key → 401."""
    resp = asgi_client.get("/api/chart-internal/BTC")
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert "error" in body or "Unauthorized" in str(body)


def test_chart_internal_invalid_key_returns_401(asgi_client):
    """GET /api/chart-internal/BTC with a wrong key → 401."""
    resp = asgi_client.get(
        "/api/chart-internal/BTC",
        headers={"X-API-Key": "definitely-not-a-real-key"},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Happy-path test — valid admin key, mocked service
# ---------------------------------------------------------------------------


def test_chart_internal_valid_key_returns_200(asgi_client, monkeypatch):
    """GET /api/chart-internal/BTC with valid key → 200, payload shape verified."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-admin-key-chart")

    # Patch at the service module level — the router does a lazy import inside
    # the handler body so we target the canonical source location.
    with patch(
        "carrac.charts.service.build_chart_payload",
        return_value=_MOCK_PAYLOAD,
    ):
        resp = asgi_client.get(
            "/api/chart-internal/BTC",
            headers={"X-Internal-Key": "test-admin-key-chart"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "BTC"
    assert "ohlcv" in body
    assert "indicators" in body


def test_chart_internal_x_internal_key_header(asgi_client, monkeypatch):
    """X-Internal-Key header is accepted (preferred for server-to-server calls)."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-internal-key")

    with patch(
        "carrac.charts.service.build_chart_payload",
        return_value=_MOCK_PAYLOAD,
    ):
        resp = asgi_client.get(
            "/api/chart-internal/ETH",
            headers={"X-Internal-Key": "test-internal-key"},
        )

    assert resp.status_code == 200, resp.text


def test_chart_internal_query_param_key_rejected(asgi_client, monkeypatch):
    """?key= query param must be rejected with 400 (security: prevents log leakage)."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-qparam-key")

    resp = asgi_client.get(
        "/api/chart-internal/ETH?key=test-qparam-key",
    )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert "URL parameter" in body.get("error", "") or "header" in body.get("error", "").lower()


# ---------------------------------------------------------------------------
# 404 path
# ---------------------------------------------------------------------------


def test_chart_internal_not_found_returns_404(asgi_client, monkeypatch):
    """build_chart_payload raising ChartNotFoundError → 404."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-404-key")

    from carrac.charts.service import ChartNotFoundError

    with patch(
        "carrac.charts.service.build_chart_payload",
        side_effect=ChartNotFoundError("No data found for UNKNOWN"),
    ):
        resp = asgi_client.get(
            "/api/chart-internal/UNKNOWN",
            headers={"X-Internal-Key": "test-404-key"},
        )

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert "error" in body


# ---------------------------------------------------------------------------
# Cache-key shape (`_cache_tf`)
# ---------------------------------------------------------------------------


def test_cache_tf_int_marker_disjoint_from_plain_chart_key():
    """The `:int` variant must never collide with a plain /api/chart key
    (which never carries `:int` — only `:col`/`:q...`/`:b<n>`), and the
    bucket suffix must land LAST so carrac.caches' bucket-stripping regex
    (which only strips a trailing `:b\\d+`) still finds it."""
    from carrac.fastapi_app.routers.chart_internal import _cache_tf
    from carrac.caches import _CHART_BUCKET_SUFFIX_RE

    # Non-bucketed (HTF) timeframe: just the marker appended.
    assert _cache_tf("1D") == "1D:int"
    assert _cache_tf("1D") != "1D"
    assert _cache_tf("1D") != "1D:col"

    # Bucketed (intraday) timeframe: marker BEFORE the trailing :b<bucket>,
    # so the alias-strip regex (anchored `:b\d+$`) still finds the bucket
    # suffix and correctly reduces to the `:int`-marked bucket-agnostic alias.
    variant = _cache_tf("4h")
    assert variant.startswith("4h:int:b")
    alias = _CHART_BUCKET_SUFFIX_RE.sub("", variant)
    assert alias == "4h:int"
    assert alias != "4h"  # still disjoint from /api/chart's own alias


# ---------------------------------------------------------------------------
# Fresh cache hit: skip build entirely
# ---------------------------------------------------------------------------


def test_chart_internal_fresh_hit_skips_build(asgi_client, monkeypatch):
    """A fresh cache entry is returned as-is; build_chart_payload is never
    called."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-hit-key")
    cached_payload = {**_MOCK_PAYLOAD, "_cached_at": int(time.time())}

    with patch(
        "carrac.fastapi_app.routers.chart_internal.get_cached_chart",
        return_value=(cached_payload, True),
    ) as mock_get, patch(
        "carrac.charts.service.build_chart_payload"
    ) as mock_build:
        resp = asgi_client.get(
            "/api/chart-internal/BTC", headers={"X-Internal-Key": "test-hit-key"}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == cached_payload
    mock_build.assert_not_called()
    mock_get.assert_called_once_with("BTC", "1D:int", False)


# ---------------------------------------------------------------------------
# Stale cache hit: serve old payload, rebuild in background
# ---------------------------------------------------------------------------


def test_chart_internal_stale_returns_old_and_schedules_one_rebuild(asgi_app, monkeypatch):
    """A stale entry is returned immediately (unchanged); exactly one
    background rebuild is scheduled and writes the rebuilt payload back
    under the SAME `:int`-marked key."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-stale-key")
    stale_payload = {**_MOCK_PAYLOAD, "_cached_at": int(time.time()) - 999}
    rebuilt_payload = {**_MOCK_PAYLOAD, "_cached_at": int(time.time()), "_rebuilt": True}

    async def _scenario():
        transport = ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            resp = await ac.get(
                "/api/chart-internal/BTC", headers={"X-Internal-Key": "test-stale-key"}
            )
            # Give the asyncio.create_task'd background rebuild a chance to
            # run to completion (it awaits asyncio.to_thread — the mock call
            # itself is instant; 0.3s is a generous margin).
            await asyncio.sleep(0.3)
            return resp

    with patch(
        "carrac.fastapi_app.routers.chart_internal.get_cached_chart",
        return_value=(stale_payload, False),
    ), patch(
        "carrac.fastapi_app.routers.chart_internal.set_cached_chart"
    ) as mock_set, patch(
        "carrac.charts.service.build_chart_payload", return_value=rebuilt_payload
    ) as mock_build:
        resp = asyncio.run(_scenario())

    assert resp.status_code == 200, resp.text
    assert resp.json() == stale_payload  # served immediately, unmodified

    mock_build.assert_called_once_with("BTC", mode="htf", timeframe="1D", days=None)
    mock_set.assert_called_once_with("BTC", "1D:int", False, rebuilt_payload)


def test_chart_internal_stale_no_stampede_on_concurrent_requests(asgi_app, monkeypatch):
    """N concurrent requests against the SAME stale key trigger exactly ONE
    background rebuild — the in-process in-flight guard de-dupes the rest.

    The mocked build blocks on a ``threading.Event`` that the test only sets
    AFTER all N HTTP responses are back — i.e. after every request has
    necessarily already run its own synchronous guard-check-and-maybe-spawn
    (that happens before the response is returned; the rebuild itself is
    fire-and-forget). This makes the assertion deterministic instead of
    relying on wall-clock luck: an instant mock actually lets a couple of
    rebuilds slip through in this in-process test transport (the first
    rebuild's guard is released before every one of the N coroutines has
    reached the check), whereas production's real build takes 6.5-9.8s,
    comfortably outlasting any realistic concurrent-arrival burst — this
    gate reproduces that "still in flight" window deterministically."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-stampede-key")
    stale_payload = {**_MOCK_PAYLOAD, "_cached_at": int(time.time()) - 999}
    N = 6
    release_build = threading.Event()

    def _slow_build(*args, **kwargs):
        release_build.wait(timeout=2)
        return dict(_MOCK_PAYLOAD)

    async def _scenario():
        transport = ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            responses = await asyncio.gather(*[
                ac.get("/api/chart-internal/BTC", headers={"X-Internal-Key": "test-stampede-key"})
                for _ in range(N)
            ])
            # Every request already got its stale response, which means
            # every one of them already ran its guard check. Now let the
            # (at most one) in-flight rebuild finish.
            release_build.set()
            await asyncio.sleep(0.3)
            return responses

    with patch(
        "carrac.fastapi_app.routers.chart_internal.get_cached_chart",
        return_value=(stale_payload, False),
    ), patch(
        "carrac.fastapi_app.routers.chart_internal.set_cached_chart"
    ), patch(
        "carrac.charts.service.build_chart_payload", side_effect=_slow_build
    ) as mock_build:
        responses = asyncio.run(_scenario())

    assert len(responses) == N
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == stale_payload for r in responses)
    mock_build.assert_called_once()  # single-flight guard held for the rest


# ---------------------------------------------------------------------------
# Cache miss: build synchronously, then cache
# ---------------------------------------------------------------------------


def test_chart_internal_miss_builds_and_caches(asgi_client, monkeypatch):
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-miss-key")

    with patch(
        "carrac.fastapi_app.routers.chart_internal.get_cached_chart", return_value=None
    ) as mock_get, patch(
        "carrac.fastapi_app.routers.chart_internal.set_cached_chart"
    ) as mock_set, patch(
        "carrac.charts.service.build_chart_payload", return_value=dict(_MOCK_PAYLOAD)
    ) as mock_build:
        resp = asgi_client.get(
            "/api/chart-internal/BTC", headers={"X-Internal-Key": "test-miss-key"}
        )

    assert resp.status_code == 200, resp.text
    mock_get.assert_called_once_with("BTC", "1D:int", False)
    mock_build.assert_called_once_with("BTC", mode="htf", timeframe="1D", days=None)
    mock_set.assert_called_once_with("BTC", "1D:int", False, dict(_MOCK_PAYLOAD))


# ---------------------------------------------------------------------------
# ?fresh=1 bypasses the lookup but still writes back
# ---------------------------------------------------------------------------


def test_chart_internal_fresh_param_bypasses_read_but_writes_back(asgi_client, monkeypatch):
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-fresh-key")

    with patch(
        "carrac.fastapi_app.routers.chart_internal.get_cached_chart"
    ) as mock_get, patch(
        "carrac.fastapi_app.routers.chart_internal.set_cached_chart"
    ) as mock_set, patch(
        "carrac.charts.service.build_chart_payload", return_value=dict(_MOCK_PAYLOAD)
    ) as mock_build:
        resp = asgi_client.get(
            "/api/chart-internal/BTC?fresh=1", headers={"X-Internal-Key": "test-fresh-key"}
        )

    assert resp.status_code == 200, resp.text
    mock_get.assert_not_called()
    mock_build.assert_called_once_with("BTC", mode="htf", timeframe="1D", days=None)
    mock_set.assert_called_once_with("BTC", "1D:int", False, dict(_MOCK_PAYLOAD))


# ---------------------------------------------------------------------------
# ?days= bypasses the cache entirely (read AND write)
# ---------------------------------------------------------------------------


def test_chart_internal_days_param_bypasses_cache_entirely(asgi_client, monkeypatch):
    """`days` alters bar count/content and isn't part of the cache key —
    honoring it must never read a payload built for a DIFFERENT day count,
    nor poison the base key by writing this one-off result under it."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-days-key")

    with patch(
        "carrac.fastapi_app.routers.chart_internal.get_cached_chart"
    ) as mock_get, patch(
        "carrac.fastapi_app.routers.chart_internal.set_cached_chart"
    ) as mock_set, patch(
        "carrac.charts.service.build_chart_payload", return_value=dict(_MOCK_PAYLOAD)
    ) as mock_build:
        resp = asgi_client.get(
            "/api/chart-internal/BTC?days=50", headers={"X-Internal-Key": "test-days-key"}
        )

    assert resp.status_code == 200, resp.text
    mock_get.assert_not_called()
    mock_build.assert_called_once_with("BTC", mode="htf", timeframe="1D", days=50)
    mock_set.assert_not_called()


def test_chart_internal_canonicalizes_uppercase_tf(asgi_app, monkeypatch):
    """Gold-standard TF canon at the chart-internal boundary: '4H' (the
    morning engine's spelling) must hit the same '4h'-keyed warm cache and
    build with the canonical spelling — uppercase requests would miss every
    chartwarm key and pay 30-60s cold builds."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-canon-key")

    async def _scenario():
        transport = ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
            return await ac.get(
                "/api/chart-internal/BTC?timeframe=4H&mode=ltf",
                headers={"X-Internal-Key": "test-canon-key"},
            )

    with patch(
        "carrac.fastapi_app.routers.chart_internal.get_cached_chart",
        return_value=None,
    ) as mock_get, patch(
        "carrac.fastapi_app.routers.chart_internal.set_cached_chart"
    ) as mock_set, patch(
        "carrac.charts.service.build_chart_payload", return_value=dict(_MOCK_PAYLOAD)
    ) as mock_build:
        resp = asyncio.run(_scenario())

    assert resp.status_code == 200, resp.text
    # Cache read/write and build all use the canonical lowercase spelling.
    # Intraday keys carry a time-derived ':b<bucket>' suffix — assert the
    # canonical prefix rather than the full key.
    read_key = mock_get.call_args[0][1]
    assert read_key.startswith("4h:int"), read_key
    mock_build.assert_called_once_with("BTC", mode="ltf", timeframe="4h", days=None)
    write_key = mock_set.call_args[0][1]
    assert write_key.startswith("4h:int"), write_key
    assert read_key == write_key
