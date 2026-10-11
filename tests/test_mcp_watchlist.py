"""Tests for watchlist_manage — already_present honesty fix.

Adding a symbol that is already on the watchlist is a no-op: nothing was
written, so the response must say ``already_present`` instead of claiming
``added``. Collaborators (the ``user_db`` singleton) are stubbed; the
handler under test — ``watchlist_manage`` — is never monkeypatched.
"""

import pytest

from carrac.mcp import tools  # noqa: F401 — ensures registration
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools.watchlist import watchlist_manage


@pytest.fixture(autouse=True)
def _bind_user(bind_mcp_user):
    """Bind a per-user id so get_mcp_user_id() doesn't raise."""
    return bind_mcp_user


def _patch_user_db(monkeypatch, initial_symbols, record_adds=None):
    """Stub the user_db singleton's watchlist methods.

    Returns the (mutable) symbol list the handler will read/write through.
    """
    import user_db as user_db_mod

    store = list(initial_symbols)

    def _get_flat_watchlist(user_id):
        return list(store)

    def _add_to_watchlist(user_id, symbol):
        if record_adds is not None:
            record_adds.append(symbol)
        if symbol not in store:
            store.append(symbol)
        return True

    monkeypatch.setattr(user_db_mod.user_db, 'get_flat_watchlist', _get_flat_watchlist)
    monkeypatch.setattr(user_db_mod.user_db, 'add_to_watchlist', _add_to_watchlist)
    return store


class TestWatchlistManageRegistered:
    def test_registered(self):
        assert 'watchlist_manage' in TOOL_REGISTRY


class TestAddAlreadyPresent:
    def test_already_present_symbol_returns_already_present_key(self, app, monkeypatch):
        _patch_user_db(monkeypatch, ['BTC', 'ETH'])

        with app.app_context():
            out = watchlist_manage({'action': 'add', 'symbol': 'BTC'})

        assert out['already_present'] == 'BTC'
        assert 'added' not in out
        assert out['symbols'] == ['BTC', 'ETH']
        assert out['count'] == 2

    def test_already_present_is_case_insensitive(self, app, monkeypatch):
        _patch_user_db(monkeypatch, ['BTC'])

        with app.app_context():
            out = watchlist_manage({'action': 'add', 'symbol': 'btc'})

        assert out['already_present'] == 'BTC'
        assert 'added' not in out

    def test_already_present_does_not_call_add(self, app, monkeypatch):
        """The core honesty check: nothing gets written when the symbol is
        already on the list — this is the no-op the response now admits to."""
        recorded = []
        _patch_user_db(monkeypatch, ['BTC'], record_adds=recorded)

        with app.app_context():
            watchlist_manage({'action': 'add', 'symbol': 'BTC'})

        assert recorded == []

    def test_already_present_skips_full_watchlist_check(self, app, monkeypatch):
        """A full watchlist (>= 200 symbols) must not block reporting that a
        symbol already on the list is already there — the already_present
        path is read-only and doesn't need capacity."""
        full = [f'SYM{i}' for i in range(200)]
        full[0] = 'BTC'
        _patch_user_db(monkeypatch, full)

        with app.app_context():
            out = watchlist_manage({'action': 'add', 'symbol': 'BTC'})

        assert out['already_present'] == 'BTC'


class TestAddNewSymbol:
    def test_new_symbol_still_returns_added(self, app, monkeypatch):
        """The happy path must be unchanged: a genuinely new symbol still
        reports `added`, not `already_present`."""
        _patch_user_db(monkeypatch, ['ETH'])

        with app.app_context():
            out = watchlist_manage({'action': 'add', 'symbol': 'SOL'})

        assert out['added'] == 'SOL'
        assert 'already_present' not in out
        assert out['symbols'] == ['ETH', 'SOL']
        assert out['count'] == 2

    def test_full_watchlist_still_blocks_new_symbol(self, app, monkeypatch):
        full = [f'SYM{i}' for i in range(200)]
        _patch_user_db(monkeypatch, full)

        with app.app_context():
            with pytest.raises(ToolError) as exc:
                watchlist_manage({'action': 'add', 'symbol': 'BTC'})
        assert 'full' in str(exc.value).lower()


class TestOnboardingConsumerUnaffected:
    """carrac/mcp/tools/onboarding.py::get_started calls
    watchlist_manage({'action': 'list'}) inside get_started(). The 'list'
    branch is untouched by this fix, but assert the wiring explicitly so a
    future change to the add/list boundary trips this test."""

    def test_onboarding_list_call_still_works(self, app, monkeypatch):
        from carrac.mcp.tools.onboarding import _safe

        _patch_user_db(monkeypatch, ['BTC', 'ETH'])

        with app.app_context():
            out = _safe(watchlist_manage, {'action': 'list'})

        assert out == {'symbols': ['BTC', 'ETH'], 'count': 2}
        assert 'added' not in out
        assert 'already_present' not in out
