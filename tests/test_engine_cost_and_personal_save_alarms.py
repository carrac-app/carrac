"""Two silences with money/product weight.

save_personal_section: a paying customer's personal report could fail to
save with only a log.warning — the user simply never gets their report.

log_engine_costs / _log_verify_cost: if _increment_global_spend fails,
the $30/day LLM cost cap silently stops seeing real spend — the exact
"cap blind to ~100% of spend" failure both docstrings describe, guarded
by nothing but a warning line nobody reads. Both now beacon to the error tracker;
neither may raise (the engine must finish the remaining users/reports).
"""
from unittest.mock import MagicMock

import morning_engine.dashboard_api as da
import morning_engine.engine as eng


def _ok_user_db(monkeypatch):
    import user_db as udb
    monkeypatch.setattr(udb.user_db, 'log_ai_usage', lambda **k: None)


def _broken_spend(monkeypatch):
    import carrac.ai.billing as billing
    def _boom(_c):
        raise RuntimeError('simulated redis outage')
    monkeypatch.setattr(billing, '_increment_global_spend', _boom)


def _client(cost=1.0):
    c = MagicMock()
    c.get_usage_summary.return_value = {
        'total_cost': cost, 'total_input_tokens': 10, 'total_output_tokens': 20}
    c.model = 'test-model'
    return c


def test_personal_save_failure_beacons_and_does_not_raise(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError('dashboard unreachable')
    monkeypatch.setattr(da.requests, 'post', _boom)
    beacons = []
    monkeypatch.setattr(da, '_ops_beacon', lambda m: beacons.append(m))

    da.save_personal_section('r1', '7', 'personal content')

    assert beacons, (
        'a paying user just silently lost their personal report — that must '
        'reach Bugsink, not a warning line'
    )


def test_engine_cost_cap_blindness_beacons(monkeypatch):
    _ok_user_db(monkeypatch)
    _broken_spend(monkeypatch)
    beacons = []
    monkeypatch.setattr(da, '_ops_beacon', lambda m: beacons.append(m))

    da.log_engine_costs('morning', primary=_client())

    assert beacons, 'the $30/day cap just went blind to real spend, silently'


def test_verify_cost_cap_blindness_beacons(monkeypatch):
    _ok_user_db(monkeypatch)
    _broken_spend(monkeypatch)
    beacons = []
    monkeypatch.setattr(da, '_ops_beacon', lambda m: beacons.append(m))

    eng._log_verify_cost('morning', 0.5)

    assert beacons


def test_healthy_paths_stay_silent(monkeypatch):
    _ok_user_db(monkeypatch)
    import carrac.ai.billing as billing
    monkeypatch.setattr(billing, '_increment_global_spend', lambda c: None)
    req = MagicMock()
    monkeypatch.setattr(da, 'requests', req)
    beacons = []
    monkeypatch.setattr(da, '_ops_beacon', lambda m: beacons.append(m))

    da.save_personal_section('r1', '7', 'personal content')
    da.log_engine_costs('morning', primary=_client())
    eng._log_verify_cost('morning', 0.5)

    assert not beacons, 'healthy runs must not cry wolf'
