import asyncio

import pytest

import carrac.curl_charts.render as render_mod
from carrac.curl_charts.grammar import Options, snap_window


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, k):
        return self.store.get(k)

    def setex(self, k, ttl, v):
        self.store[k] = v


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(render_mod, "_redis", lambda: r)
    return r


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_cache_hit_skips_render(fake_redis, monkeypatch):
    bucket = snap_window("1d")
    opts = Options()
    key = render_mod._cache_key("BTC", bucket, opts)
    fake_redis.store[key] = b"CACHED CHART\n"

    async def boom(*a, **k):
        raise AssertionError("must not render on cache hit")

    monkeypatch.setattr(render_mod, "_render_subprocess", boom)
    out = _run(render_mod.render_chart("BTC", bucket, opts))
    assert out == "CACHED CHART\n"


def test_cache_miss_renders_and_stores(fake_redis, monkeypatch):
    bucket = snap_window("1d")
    opts = Options()

    monkeypatch.setattr(render_mod, "_fetch_payload", lambda sym, b: {"ohlcv": [1]})

    async def fake_render(payload, bucket, opts):
        return "FRESH CHART\n"

    monkeypatch.setattr(render_mod, "_render_subprocess", fake_render)
    out = _run(render_mod.render_chart("BTC", bucket, opts))
    assert out == "FRESH CHART\n"
    assert any(v == b"FRESH CHART\n" for v in fake_redis.store.values())


def test_payload_error_maps_to_data_unavailable(fake_redis, monkeypatch):
    bucket = snap_window("1d")

    def boom(sym, b):
        raise RuntimeError("breaker tripped")

    monkeypatch.setattr(render_mod, "_fetch_payload", boom)
    with pytest.raises(render_mod.DataUnavailable):
        _run(render_mod.render_chart("BTC", bucket, Options()))


def test_cache_keys_differ_by_opts(fake_redis):
    bucket = snap_window("1d")
    k1 = render_mod._cache_key("BTC", bucket, Options())
    k2 = render_mod._cache_key("BTC", bucket, Options(ema=True))
    k3 = render_mod._cache_key("BTC", snap_window("10d"), Options())
    assert len({k1, k2, k3}) == 3


def test_render_failure_propagates(fake_redis, monkeypatch):
    bucket = snap_window("1d")
    monkeypatch.setattr(render_mod, "_fetch_payload", lambda sym, b: {"ohlcv": [1]})

    async def boom(payload, bucket, opts):
        raise render_mod.RenderFailed("exit 2")

    monkeypatch.setattr(render_mod, "_render_subprocess", boom)
    with pytest.raises(render_mod.RenderFailed):
        _run(render_mod.render_chart("BTC", bucket, Options()))
    assert not fake_redis.store  # failures are not cached
