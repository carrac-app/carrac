"""One source of truth for the chart cache-key suffix, and a
persistent ledger of blocking misses.

The request path (carrac/charts/routes.py::get_chart_data) and the worker's
warm path (carrac/precompute.py::_chart_warm_lookup_tf) must build the
cache-key suffix identically. Two hand-synced implementations guarded only by
a MUST-mirror comment drift silently: the warmer then populates keys no
request ever reads. Both go through ``build_chart_cache_suffix``, and the
parity test here fails if anyone re-introduces a separate builder.

The miss ledger (``record_chart_miss``) persists per-(symbol, tf) blocking
misses in a daily Redis hash: the ``docker logs --since 6h`` evidence window
dies at every deploy (each recreates the containers), and
the warm-tier re-scope must be sized from measured traffic, not guesses.
"""
import logging

from carrac.caches import CHART_MISS_LEDGER_TTL_S, record_chart_miss
from carrac.charts.routes import build_chart_cache_suffix
from carrac.precompute import _chart_warm_lookup_tf


class TestSuffixBuilder:
    def test_columnar_htf_is_col_only(self):
        assert build_chart_cache_suffix('1D', columnar=True) == ':col'

    def test_non_columnar_htf_is_empty(self):
        assert build_chart_cache_suffix('1D', columnar=False) == ''

    def test_quant_series_sorted_so_param_order_cannot_fragment(self):
        a = build_chart_cache_suffix('1D', columnar=True, quant_param='rsi,funding')
        b = build_chart_cache_suffix('1D', columnar=True, quant_param='funding, rsi')
        assert a == b == ':col:qfunding,rsi'

    def test_intraday_gets_the_candle_period_bucket(self):
        # 1h bucket at a pinned instant: 7_200_000ms / 3_600_000ms = bucket 2
        s = build_chart_cache_suffix('1h', columnar=True, now_ms=7_200_000)
        assert s == ':col:b2'

    def test_bucket_is_appended_last_after_quant(self):
        s = build_chart_cache_suffix('1h', columnar=True, quant_param='rsi',
                                     now_ms=7_200_000)
        assert s == ':col:qrsi:b2'


class TestWarmRequestParity:
    def test_warm_key_equals_request_key_for_every_warmed_tf(self):
        """The warmer warms 1h/4h/1D (core) and 1D (watchlist). For each, the
        key the warm pass populates must be the key a real fmt=col request
        looks up. Intraday keys carry a time bucket, so compute both sides
        twice and accept agreement on either attempt (a bucket boundary can
        fall between the two calls once)."""
        for tf in ('1h', '4h', '1D'):
            for _ in range(2):
                warm = _chart_warm_lookup_tf(tf)
                req = tf + build_chart_cache_suffix(tf, columnar=True)
                if warm == req:
                    break
            assert warm == req, (
                f'{tf}: warm path key {warm!r} != request path key {req!r} — '
                f'a warmed key no request ever reads'
            )


class _FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.ttls = {}

    def pipeline(self):
        return _FakePipe(self)


class _FakePipe:
    def __init__(self, r):
        self.r = r
        self.ops = []

    def hincrby(self, key, field, n):
        self.ops.append(('hincrby', key, field, n))
        return self

    def expire(self, key, ttl):
        self.ops.append(('expire', key, ttl))
        return self

    def execute(self):
        for op in self.ops:
            if op[0] == 'hincrby':
                _, key, field, n = op
                h = self.r.hashes.setdefault(key, {})
                h[field] = h.get(field, 0) + n
            else:
                _, key, ttl = op
                self.r.ttls[key] = ttl


class TestMissLedger:
    def test_a_miss_lands_in_the_daily_hash_with_a_ttl(self):
        r = _FakeRedis()
        record_chart_miss('TSLA', '1D', r=r, day='2026-08-25')
        record_chart_miss('TSLA', '1D', r=r, day='2026-08-25')
        record_chart_miss('GBPUSD', '1W', r=r, day='2026-08-25')
        h = r.hashes['mcd:chartmiss:2026-08-25']
        assert h == {'TSLA:1D': 2, 'GBPUSD:1W': 1}
        assert r.ttls['mcd:chartmiss:2026-08-25'] == CHART_MISS_LEDGER_TTL_S

    def test_a_redis_fault_never_raises(self, caplog):
        class _Broken:
            def pipeline(self):
                raise ConnectionError('redis down')

        caplog.set_level(logging.DEBUG)
        record_chart_miss('BTC', '1D', r=_Broken(), day='2026-08-25')
