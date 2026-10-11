"""Tests for MCP request-intent telemetry.

Covers:
- user_intent injected into every tool's inputSchema via the @mcp_tool
  decorator (not per-tool edits); unmet_need only on the 10 entry-point
  tools; neither field is added to `required`.
- call_tool() writes exactly one mcp_intent_log row per dispatch.
- user_ref is a pseudonymised hash, never the raw user_id.
- prev_tool records the previous tool called by the same caller.
- A telemetry/logging failure never fails the tool call itself.

The mcp_intent_log table is created by the alembic migration in
Postgres. db_pool's SQLite path used by tests has no
migration runner (see tests/fastapi/test_admin_usage.py's
_ensure_ai_usage_log_table for the established precedent) — the fixture
below creates the table directly in the pool's SQLite file.
"""
from __future__ import annotations

import pytest

from carrac.mcp import tools  # noqa: F401 — ensure all tools are registered
from carrac.mcp import runtime
from carrac.mcp.registry import ENTRY_POINT_INTENT_TOOLS, TOOL_REGISTRY, call_tool


def _ensure_mcp_intent_log_table():
    """Create mcp_intent_log in the pool DB (carrac.db in tests).

    Mirrors tests/fastapi/test_admin_usage.py's _ensure_ai_usage_log_table:
    in production this table lives in the same PostgreSQL DB created by the
    alembic migration; in tests db_pool uses a separate SQLite
    file with no migration step, so the schema is created ad hoc here.
    """
    from carrac.db_pool import get_conn

    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_intent_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tool TEXT NOT NULL,
                tier TEXT NOT NULL,
                user_ref TEXT,
                user_intent TEXT,
                unmet_need TEXT,
                latency_ms REAL NOT NULL,
                ok INTEGER NOT NULL,
                result_empty INTEGER NOT NULL DEFAULT 0,
                error_kind TEXT,
                prev_tool TEXT,
                -- Mirrors migration mcperrdet260824. This fixture
                -- is a hand-maintained copy of the Postgres schema, so a new
                -- column has to be added in BOTH places — and when it is not,
                -- the failure is silent in production: log_intent_dispatch wraps its
                -- write in `except Exception: logger.exception(...)`, so a
                -- missing column stops ALL intent logging without an alarm.
                error_detail TEXT
            )
            """
        )
        conn.commit()


@pytest.fixture
def intent_log_db(app):
    """Ensure mcp_intent_log exists in this test's isolated pool DB, and
    reset the in-process prev_tool fallback so tests don't leak sequence
    state into each other."""
    runtime._prev_tool_fallback.clear()
    _ensure_mcp_intent_log_table()
    yield
    runtime.reset_mcp_caller()
    runtime._prev_tool_fallback.clear()


def _row_count():
    from carrac.db_pool import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM mcp_intent_log").fetchone()
        return row["cnt"]


def _rows():
    from carrac.db_pool import get_conn
    with get_conn() as conn:
        return conn.execute("SELECT * FROM mcp_intent_log ORDER BY id").fetchall()


# ── Schema injection (registry.py's @mcp_tool decorator) ───────────────────


class TestSchemaInjection:
    def test_user_intent_on_every_registered_tool(self):
        assert len(TOOL_REGISTRY) > 0
        for name, tool in TOOL_REGISTRY.items():
            props = tool['inputSchema'].get('properties', {})
            assert 'user_intent' in props, f"{name} missing user_intent in schema"
            assert props['user_intent']['type'] == 'string'
            assert props['user_intent']['maxLength'] == 2048

    def test_user_intent_and_unmet_need_never_required(self):
        for name, tool in TOOL_REGISTRY.items():
            required = tool['inputSchema'].get('required', [])
            assert 'user_intent' not in required, f"{name} wrongly requires user_intent"
            assert 'unmet_need' not in required, f"{name} wrongly requires unmet_need"

    def test_unmet_need_only_on_the_ten_entry_points(self):
        for name, tool in TOOL_REGISTRY.items():
            props = tool['inputSchema'].get('properties', {})
            if name in ENTRY_POINT_INTENT_TOOLS:
                assert 'unmet_need' in props, f"{name} is an entry point but missing unmet_need"
                assert props['unmet_need']['type'] == 'string'
                assert props['unmet_need']['maxLength'] == 512
            else:
                assert 'unmet_need' not in props, f"{name} should NOT have unmet_need"

    def test_entry_point_set_matches_ticket_and_is_registered(self):
        assert ENTRY_POINT_INTENT_TOOLS == {
            'get_started', 'get_guide', 'scanner_get_screen', 'market_get_overview',
            'play_list', 'report_get_compact', 'quant_get_data', 'trade_list',
            'report_get_section', 'market_search_symbol',
        }
        for name in ENTRY_POINT_INTENT_TOOLS:
            assert name in TOOL_REGISTRY, f"entry-point tool {name!r} is not registered"

    def test_existing_schema_properties_untouched(self):
        """Injection must not clobber a tool's own declared properties/required."""
        schema = TOOL_REGISTRY['scanner_get_screen']['inputSchema']
        assert 'min_conditions' in schema['properties']
        assert 'alignment' in schema['properties']
        assert 'limit' in schema['properties']

        leadlag_schema = TOOL_REGISTRY['quant_get_leadlag_pair']['inputSchema']
        assert set(leadlag_schema['required']) == {'leader', 'target'}


# ── Dispatch logging (registry.call_tool + runtime.log_intent_dispatch) ────


class TestDispatchLogging:
    def test_one_call_writes_exactly_one_row(self, intent_log_db):
        before = _row_count()
        result = call_tool('indicator_get_dsl_reference', {'user_intent': 'learn the DSL grammar'})
        after = _row_count()

        assert after - before == 1
        assert 'engine_version' in result  # handler's real return value, unaffected

        row = _rows()[-1]
        assert row['tool'] == 'indicator_get_dsl_reference'
        assert row['user_intent'] == 'learn the DSL grammar'
        assert row['unmet_need'] is None
        assert row['ok'] in (1, True)
        assert row['latency_ms'] >= 0
        assert row['result_empty'] in (0, False)

    def test_unmet_need_logged_for_entry_point_tool(self, intent_log_db):
        # get_guide is an entry-point tool (has unmet_need) with no external
        # cache/network dependency, so it behaves deterministically in tests.
        call_tool('get_guide', {
            'topic': 'formulas',
            'user_intent': 'find bullish setups',
            'unmet_need': 'no way to filter by sector',
        })
        row = _rows()[-1]
        assert row['tool'] == 'get_guide'
        assert row['user_intent'] == 'find bullish setups'
        assert row['unmet_need'] == 'no way to filter by sector'

    def test_user_ref_is_hashed_not_raw_id(self, intent_log_db):
        runtime.set_mcp_caller(user_id=777, tier='member')
        call_tool('indicator_get_dsl_reference', {})
        row = _rows()[-1]
        assert row['tier'] == 'member'
        assert row['user_ref'] is not None
        assert row['user_ref'] != '777'
        assert '777' not in row['user_ref']
        assert len(row['user_ref']) >= 16

    def test_user_ref_stable_for_same_user_across_calls(self, intent_log_db):
        runtime.set_mcp_caller(user_id=42, tier='member')
        call_tool('indicator_get_dsl_reference', {})
        call_tool('indicator_get_dsl_reference', {})
        rows = _rows()
        assert rows[-1]['user_ref'] == rows[-2]['user_ref']

    def test_different_users_get_different_hashes(self, intent_log_db):
        runtime.set_mcp_caller(user_id=1, tier='member')
        call_tool('indicator_get_dsl_reference', {})
        runtime.set_mcp_caller(user_id=2, tier='member')
        call_tool('indicator_get_dsl_reference', {})
        rows = _rows()
        assert rows[-1]['user_ref'] != rows[-2]['user_ref']

    def test_shared_admin_key_has_null_user_ref(self, intent_log_db):
        runtime.reset_mcp_caller()  # user_id=None → shared/admin caller
        call_tool('indicator_get_dsl_reference', {})
        row = _rows()[-1]
        assert row['user_ref'] is None

    def test_prev_tool_records_caller_sequence(self, intent_log_db):
        runtime.set_mcp_caller(user_id=555, tier='member')
        call_tool('indicator_get_dsl_reference', {})
        call_tool('get_guide', {'topic': 'formulas'})
        rows = _rows()
        assert rows[-2]['tool'] == 'indicator_get_dsl_reference'
        assert rows[-2]['prev_tool'] is None  # first call by this caller
        assert rows[-1]['tool'] == 'get_guide'
        assert rows[-1]['prev_tool'] == 'indicator_get_dsl_reference'

    def test_error_kind_recorded_and_original_exception_still_raised(self, intent_log_db, monkeypatch):
        """A handler exception must propagate to the caller completely
        unchanged (same type), while still being logged as ok=False."""

        def _boom(arguments):
            raise RuntimeError('boom')

        monkeypatch.setitem(TOOL_REGISTRY['indicator_get_dsl_reference'], 'handler', _boom)

        with pytest.raises(RuntimeError, match='boom'):
            call_tool('indicator_get_dsl_reference', {})

        row = _rows()[-1]
        assert row['tool'] == 'indicator_get_dsl_reference'
        assert row['ok'] in (0, False)
        assert row['error_kind'] == 'RuntimeError'

    def test_logging_failure_never_fails_the_tool_call(self, intent_log_db, monkeypatch):
        """Hard constraint: telemetry must never fail a tool call, even if
        the DB write itself is completely broken."""
        import carrac.db_pool as db_pool_mod

        def _boom_conn():
            raise RuntimeError('db is down')

        monkeypatch.setattr(db_pool_mod, 'get_conn', _boom_conn)

        result = call_tool('indicator_get_dsl_reference', {'user_intent': 'should not crash'})
        assert 'engine_version' in result  # real result, despite logging being broken

    def test_unknown_tool_raises_keyerror_and_does_not_log(self, intent_log_db):
        before = _row_count()
        with pytest.raises(KeyError):
            call_tool('does_not_exist', {})
        after = _row_count()
        assert after == before  # routing error, not a dispatch — no row written
