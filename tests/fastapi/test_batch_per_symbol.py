"""GET /api/batch reads scanner + htf per-symbol instead of
pulling the whole 10.8MB scanner blob / 2.1MB htf blob on every call.

``_gather_and_assemble`` (carrac/fastapi_app/routers/composite.py) already
knows the requested ``assets`` list (<=MAX_BATCH_ASSETS) before firing the
scanner/htf sub-fetches — this test verifies that list is actually forwarded
into ``_get_scanner_by_symbol`` / ``_get_htf_by_symbol`` (so they can HMGET
the per-symbol Redis hash) rather than calling them with no arguments (the
old whole-blob signature).
"""

from __future__ import annotations


def test_batch_forwards_requested_assets_to_scanner_and_htf(asgi_client, member_key, monkeypatch):
    """GET /api/batch?assets=BTC,ETH&include=market,scanner must call
    _get_scanner_by_symbol(['BTC', 'ETH']) and _get_htf_by_symbol(['BTC', 'ETH'])
    — not the bare no-arg calls that pull the whole blob."""
    import carrac.composite.assemblers as asm

    captured: dict = {}

    def _fake_scanner(symbols=None):
        captured['scanner_symbols'] = symbols
        return {}

    def _fake_htf(symbols=None):
        captured['htf_symbols'] = symbols
        return {}

    monkeypatch.setattr(asm, '_get_scanner_by_symbol', _fake_scanner)
    monkeypatch.setattr(asm, '_get_htf_by_symbol', _fake_htf)

    headers, _uid = member_key
    resp = asgi_client.get(
        '/api/batch',
        params={'assets': 'BTC,ETH', 'include': 'market,scanner'},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert captured.get('scanner_symbols') == ['BTC', 'ETH']
    assert captured.get('htf_symbols') == ['BTC', 'ETH']


def test_batch_scanner_only_still_forwards_assets(asgi_client, member_key, monkeypatch):
    """include=scanner (no 'market') only fires the scanner sub-fetch, but
    it must still receive the bounded assets list, not None."""
    import carrac.composite.assemblers as asm

    captured: dict = {}

    def _fake_scanner(symbols=None):
        captured['scanner_symbols'] = symbols
        return {}

    monkeypatch.setattr(asm, '_get_scanner_by_symbol', _fake_scanner)

    headers, _uid = member_key
    resp = asgi_client.get(
        '/api/batch',
        params={'assets': 'SOL', 'include': 'scanner'},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert captured.get('scanner_symbols') == ['SOL']


def test_batch_end_to_end_uses_per_symbol_hash(asgi_client, member_key, monkeypatch):
    """Full round-trip: with the per-symbol hash populated (and the blob
    fallback wired to blow up if reached), /api/batch must still return the
    correct market+scanner data for the requested symbol — proving the hot
    path never touches the blob when the hash has what it needs."""
    import carrac.caches as caches
    import carrac.composite.assemblers as asm

    def _leaky_scanner_blob():
        raise AssertionError('get_cached_scanner (10.8MB blob) was called — hash path should short-circuit')

    class _LeakyResponseCache:
        def get(self, mode):
            raise AssertionError("response_cache.get('htf') (2.1MB blob) was called — hash path should short-circuit")

    monkeypatch.setattr(
        caches, 'get_scanner_by_symbol_hash',
        lambda symbols: {'BTC': {'symbol': 'BTC', 'long': {'total': 9, 'verdict': 'STRONG'},
                                  'short': {'total': 1, 'verdict': 'AVOID'}, 'best_verdict': 'STRONG'}},
    )
    monkeypatch.setattr(caches, 'get_cached_scanner', _leaky_scanner_blob)
    monkeypatch.setattr(
        caches, 'get_htf_by_symbol_hash',
        lambda symbols: {'BTC': {'symbol': 'BTC', 'latest_price': 91000.0, 'change_pct': 2.3}},
    )
    monkeypatch.setattr(asm, '_get_response_cache', lambda: _LeakyResponseCache())
    monkeypatch.setattr(asm, '_get_prices', lambda: {})

    headers, _uid = member_key
    resp = asgi_client.get(
        '/api/batch',
        params={'assets': 'BTC', 'include': 'market,scanner'},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    btc = body['assets']['BTC']
    assert btc['scanner']['long']['score'] == 9
    assert btc['market']['price'] == 91000.0
