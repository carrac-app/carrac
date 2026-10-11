"""Chain detail must survive a None correlation.

``/api/quant/chain`` reports ``weakest_link.r`` as ``None`` when a link has
no correlation. ``_api`` is
the collaborator stubbed here; ``fetch_quant_context`` is not patched.
"""

import logging

from morning_engine import data_assembly as da


_SNAP = {
    'regime': None,
    'chains': [
        {'chain': 'monetary_policy', 'chain_strength': 0.42,
         'weakest_link': {'from': 'DXY', 'to': 'BTC', 'r': None}},
    ],
}

_CHAIN_DETAIL = {
    'chain': 'monetary_policy',
    'links': [
        {'from': 'FED', 'to': 'DXY', 'correlation': 0.61, 'persistent': True, 'best_lag': 2},
        {'from': 'DXY', 'to': 'BTC', 'correlation': None, 'persistent': False, 'best_lag': None},
    ],
    'weakest_link': {'from': 'DXY', 'to': 'BTC', 'r': None},
}


def _stub_api(path, timeout=15, retries=2):
    if path.startswith('/api/quant/snapshot'):
        return dict(_SNAP)
    if path.startswith('/api/quant/chain?'):
        return dict(_CHAIN_DETAIL)
    return {'error': 'not stubbed'}


def test_none_correlation_keeps_chain_detail(monkeypatch, caplog):
    monkeypatch.setattr(da, '_api', _stub_api)
    with caplog.at_level(logging.WARNING, logger=da.log.name):
        out = da.fetch_quant_context()
    text = out['quant']
    assert 'Chain detail fetch failed' not in ' '.join(r.getMessage() for r in caplog.records)
    assert 'FED→DXY r=+0.610' in text
    assert 'monetary_policy:' in text, text
    assert 'Weakest: DXY→BTC' in text


def test_failure_warning_carries_the_values(monkeypatch, caplog):
    """If the formatter ever dies again, the warning must name the offending values."""
    def _bad(path, timeout=15, retries=2):
        if path.startswith('/api/quant/snapshot'):
            return dict(_SNAP)
        raise RuntimeError('boom')
    monkeypatch.setattr(da, '_api', _bad)
    with caplog.at_level(logging.WARNING, logger=da.log.name):
        da.fetch_quant_context()
    msgs = [r.getMessage() for r in caplog.records if 'Chain detail fetch failed' in r.getMessage()]
    assert msgs and 'monetary_policy' in msgs[0] and 'boom' in msgs[0]
