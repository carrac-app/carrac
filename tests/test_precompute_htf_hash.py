"""precompute.py writes the per-symbol HTF hash in the SAME
pass as the ``response_cache.set('htf', ...)`` blob write.

``warmup_cache()`` and the HTF branch of ``_precompute_loop()`` both follow:
    response_cache.set('htf', data)
    ...
    set_htf_by_symbol_hash(data)          # <- the addition under test

This test exercises the ``warmup_cache()`` call site directly (the
``_precompute_loop`` site is structurally identical — same
``response_cache.set('htf', data)`` followed by the same
``set_htf_by_symbol_hash(data)`` call — but the loop itself runs forever
and isn't practical to unit-test in isolation). Everything below
``_compute_assets_response`` is mocked out so this stays a pure wiring
test, not a re-test of the HTF compute pipeline (already covered by
tests/test_precompute_parallel.py).
"""
from __future__ import annotations

import carrac.precompute as precompute


def test_warmup_cache_writes_htf_by_symbol_hash(monkeypatch):
    """warmup_cache() must call set_htf_by_symbol_hash(data) with the exact
    same `data` dict it just wrote via response_cache.set('htf', data)."""
    htf_data = {
        'assets': [
            {'symbol': 'BTC', 'latest_price': 80000.0},
            {'symbol': 'ETH', 'latest_price': 4000.0},
        ],
        'count': 2,
    }

    monkeypatch.setattr(precompute, '_compute_assets_response', lambda mode: htf_data)
    monkeypatch.setattr('carrac.redis_cache.get_redis', lambda: None)

    # Neutralize the scanner-scoring block (a separate concern, already
    # exercised by test_caches.py's TestScannerBySymbolHash) so this test
    # stays focused on the HTF hash wiring.
    monkeypatch.setattr(
        'carrac.scanner.scorer._build_macro_data', lambda: {}, raising=False
    )
    monkeypatch.setattr(
        'carrac.scanner.scorer.score_all_assets',
        lambda assets, macro: {'results': []},
        raising=False,
    )
    monkeypatch.setattr(
        'carrac.scanner.scorer.history.upsert_daily_snapshot',
        lambda scanner_data, regime: None,
        raising=False,
    )

    calls = []
    monkeypatch.setattr('carrac.caches.set_htf_by_symbol_hash', lambda data: calls.append(data))

    precompute.warmup_cache()

    assert len(calls) == 1
    assert calls[0] == htf_data

    # The blob write itself is unaffected — response_cache.get('htf') still
    # returns the full bundle (other consumers keep working unchanged).
    cached = precompute.response_cache.get('htf')
    assert cached is not None
    assert cached['count'] == 2


def test_warmup_cache_hash_write_failure_does_not_break_warmup(monkeypatch, caplog):
    """A raising set_htf_by_symbol_hash must be swallowed (logged, not
    propagated) — matching every other best-effort write in this loop."""
    import logging

    htf_data = {'assets': [{'symbol': 'BTC', 'latest_price': 1.0}], 'count': 1}

    monkeypatch.setattr(precompute, '_compute_assets_response', lambda mode: htf_data)
    monkeypatch.setattr('carrac.redis_cache.get_redis', lambda: None)
    monkeypatch.setattr(
        'carrac.scanner.scorer._build_macro_data', lambda: {}, raising=False
    )
    monkeypatch.setattr(
        'carrac.scanner.scorer.score_all_assets',
        lambda assets, macro: {'results': []},
        raising=False,
    )
    monkeypatch.setattr(
        'carrac.scanner.scorer.history.upsert_daily_snapshot',
        lambda scanner_data, regime: None,
        raising=False,
    )

    def _boom(data):
        raise RuntimeError('redis down mid-write')

    monkeypatch.setattr('carrac.caches.set_htf_by_symbol_hash', _boom)

    with caplog.at_level(logging.WARNING, logger='carrac.precompute'):
        precompute.warmup_cache()  # must not raise

    assert any('HTF by-symbol hash write failed' in r.message for r in caplog.records)
    # The blob write still succeeded despite the hash failure.
    assert precompute.response_cache.get('htf') is not None
