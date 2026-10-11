"""Precondition — the alarm that must exist before anything
stops collecting on the strength of venue evidence.

The venue-status ingestion landed without closing this gap, and the reason was:

    "Nothing consumes binance_status for decisions yet, so it is evidence, not
     a control. This becomes a real gap the moment collection is gated on it
     — a human-reaching alarm is a precondition of that gate, not of this commit."

Two ways the evidence can evaporate silently, both covered here:

1. A SHORT exchangeInfo payload. `info.get('symbols')` truthiness passes it,
   and every ticker absent from it maps to 'unknown' — so one degraded fetch
   erases the accumulated verdicts (e.g. 13 `suspended`) with no
   error anywhere.
2. A payload that IS healthy but whose contents changed, dropping a material
   share of known verdicts.
"""

import pytest

import instrument_master
from carrac import precompute
from instrument_master import InstrumentRecord


def _records(n_trading=100, n_suspended=13, n_unknown=50):
    out = {}
    i = 0
    for status, count in (('trading', n_trading), ('suspended', n_suspended),
                          ('unknown', n_unknown)):
        for _ in range(count):
            i += 1
            stem = f'SYM{i}'
            out[stem] = InstrumentRecord(id=stem, ticker=stem, cache_stem=stem,
                                         binance_status=status)
    return out


def _payload(n):
    return {'symbols': [{'symbol': f'S{i}USDT', 'status': 'TRADING'}
                        for i in range(n)]}


# ── 1. Payload health floor ───────────────────────────────────────────────────

@pytest.mark.parametrize('payload,healthy', [
    (_payload(3681), True),    # a real production response
    (_payload(1000), True),    # exactly the floor
    (_payload(999), False),    # short — the case truthiness lets through
    (_payload(3), False),
    ({'symbols': []}, False),
    ({}, False),
    (None, False),
    ({'symbols': 'not-a-list'}, False),
])
def test_only_a_demonstrably_healthy_payload_is_usable(payload, healthy):
    assert instrument_master.is_healthy_exchange_info(payload) is healthy


def test_a_short_payload_is_the_case_the_old_check_missed():
    """The case this floor exists for: truthy but useless."""
    short = _payload(3)
    assert bool(short.get('symbols')) is True, "a bare truthiness check would accept this payload"
    assert instrument_master.is_healthy_exchange_info(short) is False


# ── 2. Coverage accounting ────────────────────────────────────────────────────

def test_coverage_counts_only_positive_venue_statements_as_known():
    cov = instrument_master.venue_status_coverage(_records())
    assert cov['trading'] == 100 and cov['suspended'] == 13
    assert cov['unknown'] == 50
    assert cov['known'] == 113        # unknown is NOT evidence
    assert cov['total'] == 163


# ── 3. The alarm itself ───────────────────────────────────────────────────────

def _fire(monkeypatch, before, after):
    sent = []
    monkeypatch.setattr('carrac.notifications.ntfy.send_ntfy',
                        lambda **kw: sent.append(kw))
    precompute._alarm_on_venue_coverage_loss(before, after)
    return sent


def test_a_collapse_pages_a_human(monkeypatch, caplog):
    before = instrument_master.venue_status_coverage(_records())            # known 113
    after = instrument_master.venue_status_coverage(_records(0, 0, 163))    # known 0
    with caplog.at_level('ERROR'):
        sent = _fire(monkeypatch, before, after)
    assert sent, "a coverage collapse must reach a human, not just a log line"
    assert '113' in sent[0]['body'] and 'COLLAPSED' in sent[0]['body']
    assert any('venue-status coverage COLLAPSED' in r.message
               for r in caplog.records), "and must reach Bugsink too"


def test_a_normal_reclassification_is_silent(monkeypatch):
    """A venue moving a few pairs must not page anyone — that is the noise
    that turns a guard into something you learn to ignore."""
    before = instrument_master.venue_status_coverage(_records(100, 13, 50))
    after = instrument_master.venue_status_coverage(_records(95, 13, 55))
    assert _fire(monkeypatch, before, after) == []


def test_a_GAIN_is_silent(monkeypatch):
    before = instrument_master.venue_status_coverage(_records(100, 13, 50))
    after = instrument_master.venue_status_coverage(_records(150, 13, 0))
    assert _fire(monkeypatch, before, after) == []


def test_a_tiny_store_is_never_judged(monkeypatch):
    """Below the floor the ratio is noise; refuse to conclude rather than guess."""
    before = instrument_master.venue_status_coverage(_records(5, 0, 0))
    after = instrument_master.venue_status_coverage(_records(0, 0, 5))
    assert _fire(monkeypatch, before, after) == []
