"""check_formula_alerts must fire ONCE on a false→true transition, not on every
check cycle.

Pins the dedup client import: `_get_redis` must come from `carrac.caches`.
Importing it from a module that doesn't export it, with the ImportError
swallowed, leaves `r` always None, so the false→true hysteresis never runs and
a persistently-met formula (e.g. XRP `close < 1.3443`) re-fires every ~30 min.
This test fails if `r` is None (double-fire).
"""
import pytest

import carrac.indicator_state as ist
from carrac.alerts import store


@pytest.fixture(autouse=True)
def _alert_db(tmp_path, rebind_db):
    rebind_db(tmp_path / 'db')


class _FakeRedis:
    """Minimal bytes-returning Redis stand-in (decode_responses=False semantics)."""
    def __init__(self):
        self.store = {}
    def get(self, k):
        return self.store.get(k)
    def set(self, k, v, ex=None):
        self.store[k] = v.encode() if isinstance(v, str) else v
    def ttl(self, k):
        return 100


def test_formula_alert_fires_once_for_persistent_condition(monkeypatch):
    import push_notifications as pn
    import carrac.caches as caches
    import carrac.formula_eval as fe

    alert = {
        'id': 42, 'alert_type': 'formula', 'active': True,
        'frequency': 'once_per_bar', 'symbol': 'XRP', 'timeframe': '1d',
        'expression': 'close < 1.3443', 'user_id': '1',
    }
    fires = {'n': 0}
    store.insert(alert)
    monkeypatch.setattr(pn, 'send_push_notification',
                        lambda **k: fires.__setitem__('n', fires['n'] + 1))
    monkeypatch.setattr(pn, '_send_user_notifications', lambda *a, **k: None)
    monkeypatch.setattr(fe, 'evaluate_formula',
                        lambda *a, **k: {'met': True, 'evidence': 'close < 1.3443 = 1', 'error': None})
    fake = _FakeRedis()  # SHARED across calls so state persists, like real Redis
    monkeypatch.setattr(caches, '_get_redis', lambda: fake)

    ist.check_formula_alerts()   # condition met, no prior state → fires once
    ist.check_formula_alerts()   # condition still met, prev state '1' → suppressed
    ist.check_formula_alerts()   # still suppressed

    assert fires['n'] == 1, f"persistent formula alert re-fired {fires['n']}× (dedup broken)"


def _wire(monkeypatch, redis_obj, fires):
    import push_notifications as pn
    import carrac.caches as caches
    import carrac.formula_eval as fe
    alert = {
        'id': 43, 'alert_type': 'formula', 'active': True,
        'frequency': 'once_per_bar', 'symbol': 'XRP', 'timeframe': '1d',
        'expression': 'close < 1.3443', 'user_id': '1',
    }
    store.insert(alert)
    monkeypatch.setattr(pn, 'send_push_notification',
                        lambda **k: fires.__setitem__('n', fires['n'] + 1))
    monkeypatch.setattr(pn, '_send_user_notifications', lambda *a, **k: None)
    monkeypatch.setattr(fe, 'evaluate_formula',
                        lambda *a, **k: {'met': True, 'evidence': 'x', 'error': None})
    monkeypatch.setattr(caches, '_get_redis', lambda: redis_obj)


class TestADedupOutageDoesNotSpamOrPassSilently:
    """The three bare passes around the dedup store
    would let a Redis outage silently disable the false→true hysteresis (every
    repeating formula alert re-firing every cycle). Rule: no dedup state, no fire — skip loudly, retry
    next cycle."""

    def test_redis_unavailable_skips_the_cycle_and_alarms(self, monkeypatch):
        fires = {'n': 0}
        _wire(monkeypatch, None, fires)
        beacons = []
        monkeypatch.setattr(ist, '_dedup_beacon', lambda m: beacons.append(m))

        ist.check_formula_alerts()
        ist.check_formula_alerts()

        assert fires['n'] == 0, (
            'firing without the dedup store is how every repeating alert '
            're-fired every cycle in prod'
        )
        assert beacons, 'a disabled dedup store must reach Bugsink, not vanish'

    def test_a_state_read_blip_skips_that_alert_and_alarms(self, monkeypatch):
        class _BrokenRedis(_FakeRedis):
            def get(self, k):
                raise RuntimeError('simulated redis blip')
        fires = {'n': 0}
        _wire(monkeypatch, _BrokenRedis(), fires)
        beacons = []
        monkeypatch.setattr(ist, '_dedup_beacon', lambda m: beacons.append(m))

        ist.check_formula_alerts()

        assert fires['n'] == 0, 'no readable prior state → firing risks a re-fire storm'
        assert beacons

    def test_a_state_write_blip_withholds_the_fire(self, monkeypatch):
        class _WriteBrokenRedis(_FakeRedis):
            def set(self, k, v, ex=None):
                raise RuntimeError('simulated redis write blip')
        fires = {'n': 0}
        _wire(monkeypatch, _WriteBrokenRedis(), fires)
        beacons = []
        monkeypatch.setattr(ist, '_dedup_beacon', lambda m: beacons.append(m))

        ist.check_formula_alerts()

        assert fires['n'] == 0, (
            'an unrecorded fire re-fires next cycle — no state write, no fire'
        )
        assert beacons
