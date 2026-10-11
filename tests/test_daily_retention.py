"""The worker's daily retention prunes actually run, and are isolated.

Why this file exists: `ai_conversations` claimed a 90-day retention whose only
caller was a PROBABILISTIC prune fired on ~1% of new conversation writes, inside
`except Exception: pass`. On a product with almost no AI-chat traffic that
effectively never ran — prod held rows 155 days old against the stated 90 — and
nothing anywhere could have told you. Retention that depends on write volume is
not a retention period.

The prunes now live in `worker._run_daily_retention()`, extracted from the
scheduler loop precisely so the SET of them is assertable without starting a
thread or waiting on the daily cadence. A prune dropped from that function is
now a failing test rather than a silent gap.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def retention_spies(monkeypatch):
    """Replace each prune with a recorder. Returns the call log.

    Patches the prunes at their source modules — `_run_daily_retention` imports
    them inside the function body, so the module attribute is what it resolves.
    """
    calls: list[str] = []

    import carrac.mcp.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, 'purge_old_intent_log',
                        lambda *a, **k: calls.append('intent_log'))

    import carrac.content_db as cdb_mod

    class _FakeDrawingDB:
        def prune_old_source_briefings(self, *a, **k):
            calls.append('source_briefings')

    monkeypatch.setattr(cdb_mod, 'get_content_db', lambda: _FakeDrawingDB())

    import user_db as udb_mod
    monkeypatch.setattr(udb_mod.user_db, 'prune_old_conversations',
                        lambda days=None: calls.append(f'conversations:{days}'))

    return calls


def test_every_daily_prune_runs(retention_spies):
    import worker
    worker._run_daily_retention()

    assert 'intent_log' in retention_spies
    assert 'source_briefings' in retention_spies
    assert 'conversations:90' in retention_spies, (
        "ai_conversations is not being pruned on the daily schedule — this is "
        "the exact gap found earlier, where the 90-day claim was enforced "
        "only by a 1%-of-writes coin flip"
    )
    assert len(retention_spies) == 3, f"unexpected prune set: {retention_spies}"


@pytest.mark.parametrize('failing', ['intent_log', 'source_briefings', 'conversations'])
def test_one_failing_prune_does_not_skip_the_others(retention_spies, monkeypatch, failing):
    """Each prune is independent. Before the extraction, a raise inside the
    briefings block was caught, but anything thrown by the intent-log prune
    escaped to the loop's outer handler and skipped every prune after it —
    silently, once a day, forever."""
    def _boom(*a, **k):
        raise RuntimeError(f'{failing} is broken')

    if failing == 'intent_log':
        import carrac.mcp.runtime as runtime_mod
        monkeypatch.setattr(runtime_mod, 'purge_old_intent_log', _boom)
    elif failing == 'source_briefings':
        import carrac.content_db as cdb_mod
        monkeypatch.setattr(cdb_mod, 'get_content_db', _boom)
    else:
        import user_db as udb_mod
        monkeypatch.setattr(udb_mod.user_db, 'prune_old_conversations', _boom)

    import worker
    worker._run_daily_retention()  # must not raise

    # The other two still ran.
    assert len(retention_spies) == 2, (
        f"a failing {failing} prune took others down with it: {retention_spies}"
    )


def test_a_failing_prune_is_logged_at_error_not_swallowed(retention_spies, monkeypatch, caplog):
    """`except Exception: pass` around a retention job is a decision not to
    know it stopped working — which is how this class of defect survives."""
    import carrac.mcp.runtime as runtime_mod

    def _boom(*a, **k):
        raise RuntimeError('intent log prune exploded')

    monkeypatch.setattr(runtime_mod, 'purge_old_intent_log', _boom)

    import worker
    with caplog.at_level('ERROR'):
        worker._run_daily_retention()

    assert any('intent log prune exploded' in r.message or
               'intent log prune exploded' in str(r.getMessage())
               for r in caplog.records), (
        f"the failure was not logged at error: {[r.getMessage() for r in caplog.records]}"
    )


def test_the_probabilistic_prune_is_gone_from_the_write_path(retention_spies):
    """Pins that the prune call does not fire inside create_conversation on a
    coin flip; if it comes back, retention silently depends on traffic again."""
    import inspect
    import user_db as udb_mod

    src = inspect.getsource(udb_mod.UserDatabase.create_conversation)
    assert 'prune_old_conversations' not in src, (
        "the probabilistic prune is back inside the conversation write path"
    )
