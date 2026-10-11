"""Engine cost logging must fold report-generation spend into the global daily
budget counter IN-PROCESS.

The engine runs inside the app container (docker exec carrac), so it can write
the redis counter + ai_usage_log directly. The previous HTTP POST to
/api/engine/log-cost used a 5s timeout and swallowed every failure, so under the
heavy report-generation load the cost silently never reached the counter and the
$30 cap / cost alert stayed blind to ~100% of real spend.
"""
from morning_engine.dashboard_api import log_engine_costs


class _FakeClient:
    def __init__(self, cost, model="claude-sonnet-4-6", tin=1000, tout=200):
        self._cost, self.model, self._tin, self._tout = cost, model, tin, tout

    def get_usage_summary(self):
        return {"total_cost": self._cost,
                "total_input_tokens": self._tin,
                "total_output_tokens": self._tout}


def _no_http(monkeypatch):
    posted = []
    monkeypatch.setattr("morning_engine.dashboard_api.requests.post",
                        lambda *a, **k: posted.append(k))
    return posted


def test_engine_cost_increments_global_spend_in_process(monkeypatch):
    import carrac.ai.billing as billing
    import user_db as user_db_mod

    incremented, logged = [], []
    monkeypatch.setattr(billing, "_increment_global_spend",
                        lambda cents: incremented.append(cents))
    monkeypatch.setattr(user_db_mod.user_db, "log_ai_usage",
                        lambda **kw: logged.append(kw))
    posted = _no_http(monkeypatch)

    log_engine_costs("dispatch", sonnet=_FakeClient(0.25))

    # Folded into the budget counter in-process — no HTTP hop.
    assert incremented == [25]
    assert posted == []
    # And recorded for admin analytics with the engine feature + model.
    assert logged and logged[0]["feature"] == "engine_dispatch"
    assert logged[0]["cost_cents"] == 25
    assert logged[0]["model"] == "claude-sonnet-4-6"


def test_engine_cost_skips_none_and_zero_cost_clients(monkeypatch):
    import carrac.ai.billing as billing
    import user_db as user_db_mod

    incremented = []
    monkeypatch.setattr(billing, "_increment_global_spend",
                        lambda cents: incremented.append(cents))
    monkeypatch.setattr(user_db_mod.user_db, "log_ai_usage", lambda **kw: None)
    posted = _no_http(monkeypatch)

    log_engine_costs("dispatch", opus=None, sonnet=_FakeClient(0.0))

    assert incremented == []
    assert posted == []


def test_engine_cost_increment_failure_is_logged_not_swallowed(monkeypatch, caplog):
    """A counter-write failure must be visible (warning), not silently dropped."""
    import logging
    import carrac.ai.billing as billing
    import user_db as user_db_mod

    def _boom(_cents):
        raise RuntimeError("redis down")
    monkeypatch.setattr(billing, "_increment_global_spend", _boom)
    monkeypatch.setattr(user_db_mod.user_db, "log_ai_usage", lambda **kw: None)
    _no_http(monkeypatch)

    with caplog.at_level(logging.WARNING):
        log_engine_costs("dispatch", sonnet=_FakeClient(0.25))

    assert "global-spend" in caplog.text.lower()
