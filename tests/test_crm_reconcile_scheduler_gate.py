"""The CRM reconcile scheduler must not start when EspoCRM is
unconfigured (the retired-on-prod state).

If `_start_crm_reconcile_scheduler()` always spawned the
hourly-wake thread, then when the 7-day cadence came due it called
`run_reconciliation()`, which called `_espocrm_request()` for each of the 4
TargetLists — but `_espocrm_request()` no-ops (returns None, no exception)
when ESPOCRM_URL/ESPOCRM_API_KEY are empty, so every lookup looked exactly
like a genuinely missing TargetList. That would produce 4 separate
"TargetList '...' not found — skipping" ERRORs in error tracking: the
CRM was never actually contacted, so the run should never start.

These tests call `worker._start_crm_reconcile_scheduler()` directly rather
than running the whole worker `main()` (which brings up the full precompute /
cache / DB stack) — the gate itself is what's under test.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def espo_env(monkeypatch):
    """Set/clear the two EspoCRM config knobs on carrac.config.Config."""
    def _apply(url, key):
        import carrac.config as config_mod
        monkeypatch.setattr(config_mod.Config, 'ESPOCRM_URL', url)
        monkeypatch.setattr(config_mod.Config, 'ESPOCRM_API_KEY', key)
    return _apply


def test_scheduler_does_not_start_when_espo_unconfigured(espo_env, caplog):
    """The retired state: both env vars empty."""
    espo_env('', '')
    import worker

    with caplog.at_level('INFO'):
        thread = worker._start_crm_reconcile_scheduler()

    assert thread is None, (
        "scheduler thread was started even though EspoCRM is unconfigured — "
        "this is exactly the defect: a retired integration still "
        "firing and reporting bogus per-TargetList errors"
    )
    assert any('CRM reconcile scheduler disabled' in r.getMessage()
               for r in caplog.records), (
        "disabling the scheduler must log ONE clear info line, not just go quiet"
    )


@pytest.mark.parametrize('url,key', [('', 'some-key'), ('https://espo.example', '')])
def test_scheduler_does_not_start_when_only_partially_configured(espo_env, url, key):
    espo_env(url, key)
    import worker

    thread = worker._start_crm_reconcile_scheduler()

    assert thread is None


def test_scheduler_starts_when_espo_configured(espo_env):
    espo_env('https://espo.example', 'test-api-key')
    import worker

    thread = worker._start_crm_reconcile_scheduler()
    try:
        assert thread is not None, "scheduler must still start when EspoCRM is configured"
        assert thread.is_alive()
        assert thread.daemon
    finally:
        # Tear down cleanly so the daemon thread doesn't outlive the test run.
        worker._stop_crm_reconcile.set()
        thread.join(timeout=2)
        worker._stop_crm_reconcile.clear()
