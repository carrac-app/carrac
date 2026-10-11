"""Tests for reports + new condition types."""

import pytest

from carrac.mcp import tools  # noqa
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError
from carrac.conditions import (
    ALL_CONDITION_TYPES,
    CONDITION_SCHEMAS,
    _EVALUATORS,
    validate_condition,
)


class TestNewConditionTypes:
    def test_scanner_comparison_registered(self):
        assert 'scanner_comparison' in ALL_CONDITION_TYPES
        assert 'scanner_comparison' in _EVALUATORS
        assert 'scanner_comparison' in CONDITION_SCHEMAS

    def test_cross_play_reference_registered(self):
        assert 'cross_play_reference' in ALL_CONDITION_TYPES
        assert 'cross_play_reference' in _EVALUATORS
        assert 'cross_play_reference' in CONDITION_SCHEMAS

    def test_scanner_comparison_validates(self):
        ok, err = validate_condition({
            'id': 'r1',
            'type': 'scanner_comparison',
            'params': {'asset_a': 'BTC', 'asset_b': 'ETH',
                       'side': 'long', 'relation': 'gt'},
        })
        assert ok, err

    def test_scanner_comparison_invalid_side_rejected(self):
        ok, err = validate_condition({
            'id': 'r2',
            'type': 'scanner_comparison',
            'params': {'asset_a': 'BTC', 'asset_b': 'ETH',
                       'side': 'banana', 'relation': 'gt'},
        })
        assert ok is False
        assert 'side' in err

    def test_scanner_comparison_missing_required_rejected(self):
        ok, err = validate_condition({
            'id': 'r3',
            'type': 'scanner_comparison',
            'params': {'asset_a': 'BTC'},  # missing fields
        })
        assert ok is False
        assert 'missing required' in err

    def test_cross_play_reference_validates(self):
        ok, err = validate_condition({
            'id': 'r4',
            'type': 'cross_play_reference',
            'params': {'play_id': 1, 'target_status': 'acting'},
        })
        assert ok, err

    def test_cross_play_reference_invalid_status_rejected(self):
        ok, err = validate_condition({
            'id': 'r5',
            'type': 'cross_play_reference',
            'params': {'play_id': 1, 'target_status': 'flying'},
        })
        assert ok is False


class TestScannerComparisonEvaluator:
    def test_basic_comparison(self, monkeypatch):
        from carrac import caches as caches_mod
        monkeypatch.setattr(
            caches_mod, 'get_cached_scanner',
            lambda: {'results': [
                {'symbol': 'BTC', 'long': {'total': 80}, 'short': {'total': 20}},
                {'symbol': 'ETH', 'long': {'total': 60}, 'short': {'total': 40}},
            ]},
        )
        fn = _EVALUATORS['scanner_comparison']
        met, ev = fn({'asset_a': 'BTC', 'asset_b': 'ETH',
                      'side': 'long', 'relation': 'gt'})
        assert met is True
        assert 'BTC' in ev and 'ETH' in ev

    def test_reverse_comparison(self, monkeypatch):
        from carrac import caches as caches_mod
        monkeypatch.setattr(
            caches_mod, 'get_cached_scanner',
            lambda: {'results': [
                {'symbol': 'BTC', 'long': {'total': 80}},
                {'symbol': 'ETH', 'long': {'total': 60}},
            ]},
        )
        fn = _EVALUATORS['scanner_comparison']
        met, _ = fn({'asset_a': 'ETH', 'asset_b': 'BTC',
                     'side': 'long', 'relation': 'gt'})
        assert met is False

    def test_margin_pct_applied(self, monkeypatch):
        from carrac import caches as caches_mod
        # BTC=80, ETH=70. Without margin, BTC > ETH. With 20% margin: ETH*1.2 = 84.
        monkeypatch.setattr(
            caches_mod, 'get_cached_scanner',
            lambda: {'results': [
                {'symbol': 'BTC', 'long': {'total': 80}},
                {'symbol': 'ETH', 'long': {'total': 70}},
            ]},
        )
        fn = _EVALUATORS['scanner_comparison']
        met_no_margin, _ = fn({'asset_a': 'BTC', 'asset_b': 'ETH',
                               'side': 'long', 'relation': 'gt'})
        assert met_no_margin is True
        met_margin, _ = fn({'asset_a': 'BTC', 'asset_b': 'ETH',
                            'side': 'long', 'relation': 'gt', 'margin_pct': 20})
        assert met_margin is False

    def test_missing_data_returns_false(self, monkeypatch):
        from carrac import caches as caches_mod
        monkeypatch.setattr(caches_mod, 'get_cached_scanner', lambda: {'results': []})
        fn = _EVALUATORS['scanner_comparison']
        met, ev = fn({'asset_a': 'BTC', 'asset_b': 'ETH',
                      'side': 'long', 'relation': 'gt'})
        assert met is False
        assert 'missing scanner data' in ev


class TestCrossPlayReferenceEvaluator:
    def test_status_match(self, monkeypatch):
        from carrac.plays import models as plays_mod
        monkeypatch.setattr(
            plays_mod, 'get_play',
            lambda pid, user_id=None: {'id': pid, 'status': 'acting'},
        )
        fn = _EVALUATORS['cross_play_reference']
        met, ev = fn({'play_id': 1, 'target_status': 'acting'})
        assert met is True
        assert 'acting' in ev

    def test_status_mismatch(self, monkeypatch):
        from carrac.plays import models as plays_mod
        monkeypatch.setattr(
            plays_mod, 'get_play',
            lambda pid, user_id=None: {'id': pid, 'status': 'watching'},
        )
        fn = _EVALUATORS['cross_play_reference']
        met, _ = fn({'play_id': 1, 'target_status': 'acting'})
        assert met is False

    def test_unknown_play_returns_false(self, monkeypatch):
        from carrac.plays import models as plays_mod
        monkeypatch.setattr(plays_mod, 'get_play', lambda pid, user_id=None: None)
        fn = _EVALUATORS['cross_play_reference']
        met, ev = fn({'play_id': 999, 'target_status': 'acting'})
        assert met is False
        assert 'not found' in ev


class TestGetMorningReportRaw:
    def test_registered(self):
        assert 'report_get_raw' in TOOL_REGISTRY

    def test_unknown_type_raises(self):
        fn = TOOL_REGISTRY['report_get_raw']['handler']
        with pytest.raises(ToolError):
            fn({'type': 'made_up'})

    def test_no_report_found_raises(self, monkeypatch):
        from carrac import content_db as cdb
        class FakeDB:
            def get_latest_report(self, report_type=None):
                return None
        monkeypatch.setattr(cdb, 'get_content_db', lambda: FakeDB())
        fn = TOOL_REGISTRY['report_get_raw']['handler']
        with pytest.raises(ToolError) as exc:
            fn({})
        # The default MCP caller (no caller set) is free-tier, so 'morning'
        # resolves to 'general' and — crucially — does NOT fall back to the
        # operator 'morning' report. With no general row, it reports general.
        assert 'No general report' in str(exc.value)

    def test_returns_full_report(self, monkeypatch):
        from datetime import datetime
        from carrac import content_db as cdb
        class FakeDB:
            def get_latest_report(self, report_type=None):
                return {
                    'id': 1,
                    'report_type': 'morning',
                    'content': '<h1>Brief</h1>',
                    'summary': 'Markets are mixed.',
                    'assets_included': ['BTC', 'ETH'],
                    'created_at': datetime(2026, 4, 18, 6, 0, 0),
                }
        monkeypatch.setattr(cdb, 'get_content_db', lambda: FakeDB())
        fn = TOOL_REGISTRY['report_get_raw']['handler']
        out = fn({'type': 'morning'})
        assert out['report']['id'] == 1
        assert out['report']['summary'] == 'Markets are mixed.'
        # ISO format
        assert isinstance(out['report']['created_at'], str)
        assert '2026-04-18' in out['report']['created_at']


class TestReportFreshnessFallback:
    """Pins the dead-'morning'-pipeline behaviour: report_get_raw
    (and get_started, which calls it) must not silently hand back a stale or
    absent 'morning' row as if it were the platform's live state — it must
    substitute the freshest available SHARED report instead, and say so."""

    def _admin(self, monkeypatch):
        import carrac.mcp.runtime as rt
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'admin')

    def _db_with(self, morning=None, general=None, afternoon=None, weekly=None):
        rows = {'morning': morning, 'general': general,
                'afternoon': afternoon, 'weekly': weekly}

        class FakeDB:
            def get_latest_report(self, report_type=None):
                return rows.get(report_type)
        return FakeDB()

    def test_stale_morning_falls_back_to_fresh_general(self, monkeypatch):
        """A 'morning' row exists but is weeks old (dead
        pipeline). A fresh 'general' row exists. The admin caller — who is
        entitled to the real personal 'morning' report — must still receive
        the fresh general report, not the dead 32-day-old row, with the
        substitution flagged rather than hidden."""
        from datetime import datetime, timedelta
        from carrac import content_db as cdb
        self._admin(monkeypatch)
        db = self._db_with(
            morning={
                'id': 277, 'report_type': 'morning',
                'content': 'STALE PERSONAL', 'html': 'STALE PERSONAL',
                'summary': 'old', 'assets_included': [],
                'created_at': datetime.utcnow() - timedelta(days=32),
            },
            general={
                'id': 350, 'report_type': 'general',
                'content': 'FRESH GENERAL', 'html': 'FRESH GENERAL',
                'summary': 'fresh', 'assets_included': [],
                'created_at': datetime.utcnow() - timedelta(hours=2),
            },
        )
        monkeypatch.setattr(cdb, 'get_content_db', lambda: db)

        fn = TOOL_REGISTRY['report_get_raw']['handler']
        out = fn({'type': 'morning'})['report']
        assert out['id'] == 350
        assert out['report_type'] == 'general'
        assert out['content'] == 'FRESH GENERAL'
        assert out['requested_type'] == 'morning'
        assert out['is_fallback'] is True
        assert out['is_stale'] is False
        assert out['age_hours'] is not None and out['age_hours'] < 54

    def test_absent_morning_falls_back_to_fresh_general(self, monkeypatch):
        """Same guarantee when the 'morning' pipeline has never produced a
        row at all (not just stale)."""
        from datetime import datetime, timedelta
        from carrac import content_db as cdb
        self._admin(monkeypatch)
        db = self._db_with(
            morning=None,
            general={
                'id': 350, 'report_type': 'general',
                'content': 'FRESH GENERAL', 'html': 'FRESH GENERAL',
                'summary': 'fresh', 'assets_included': [],
                'created_at': datetime.utcnow() - timedelta(hours=2),
            },
        )
        monkeypatch.setattr(cdb, 'get_content_db', lambda: db)

        fn = TOOL_REGISTRY['report_get_raw']['handler']
        out = fn({'type': 'morning'})['report']
        assert out['id'] == 350
        assert out['requested_type'] == 'morning'
        assert out['is_fallback'] is True
        assert out['is_stale'] is False

    def test_all_pipelines_stale_returns_original_flagged_not_fabricated(self, monkeypatch):
        """If every candidate is stale/absent, the tool must not invent
        freshness — it returns the original (stale) result with is_stale=True
        rather than silently picking an equally-dead substitute."""
        from datetime import datetime, timedelta
        from carrac import content_db as cdb
        self._admin(monkeypatch)
        db = self._db_with(
            morning={
                'id': 277, 'report_type': 'morning',
                'content': 'STALE PERSONAL', 'html': 'STALE PERSONAL',
                'summary': 'old', 'assets_included': [],
                'created_at': datetime.utcnow() - timedelta(days=32),
            },
            general={
                'id': 200, 'report_type': 'general',
                'content': 'ALSO STALE', 'html': 'ALSO STALE',
                'summary': 'also old', 'assets_included': [],
                'created_at': datetime.utcnow() - timedelta(days=10),
            },
        )
        monkeypatch.setattr(cdb, 'get_content_db', lambda: db)

        fn = TOOL_REGISTRY['report_get_raw']['handler']
        out = fn({'type': 'morning'})['report']
        assert out['id'] == 277  # unchanged — nothing fresher was available
        assert out['is_fallback'] is False
        assert out['is_stale'] is True

    def test_fresh_morning_is_not_touched(self, monkeypatch):
        """No pipeline problem: a fresh 'morning' row must be served as-is,
        never swapped for the shared report."""
        from datetime import datetime, timedelta
        from carrac import content_db as cdb
        self._admin(monkeypatch)
        db = self._db_with(
            morning={
                'id': 500, 'report_type': 'morning',
                'content': 'FRESH PERSONAL', 'html': 'FRESH PERSONAL',
                'summary': 'fresh', 'assets_included': [],
                'created_at': datetime.utcnow() - timedelta(hours=3),
            },
            general={
                'id': 501, 'report_type': 'general',
                'content': 'FRESH GENERAL', 'html': 'FRESH GENERAL',
                'summary': 'fresh', 'assets_included': [],
                'created_at': datetime.utcnow() - timedelta(hours=1),
            },
        )
        monkeypatch.setattr(cdb, 'get_content_db', lambda: db)

        fn = TOOL_REGISTRY['report_get_raw']['handler']
        out = fn({'type': 'morning'})['report']
        assert out['id'] == 500
        assert out['is_fallback'] is False
        assert out['is_stale'] is False


class TestGetReportOperatorGate:
    """analysis.py:report_get_compact — security. Calling
    db.get_latest_report(report_type=...) directly would bypass the tier
    gating, freshness fallback, and operator-neutral sanitization that
    report_get_raw/report_get_section implement, so any member-tier caller
    could request type='morning' and receive the operator's own personal
    report (positions/alerts woven into its prose) verbatim. This pins that
    report_get_compact must resolve
    'morning' to the shared 'general' report for every non-admin caller,
    exactly like report_get_raw."""

    def _db_with(self, morning=None, general=None):
        rows = {'morning': morning, 'general': general}

        class FakeDB:
            def get_latest_report(self, report_type=None):
                return rows.get(report_type)
        return FakeDB()

    def test_non_admin_morning_request_gets_general_not_operator_report(self, monkeypatch):
        """The exact leak scenario: a non-admin caller asks for
        type='morning'. Even though a fresh operator 'morning' row exists
        (carrying the operator's own position language), the caller must
        be served the shared 'general' report instead — never the
        operator row, and the substitution must be disclosed."""
        from datetime import datetime, timedelta
        from carrac import content_db as cdb
        import carrac.mcp.tools.reports as reports_mod

        monkeypatch.setattr(reports_mod, '_caller_is_admin', lambda: False)
        monkeypatch.setattr(reports_mod, '_caller_is_free', lambda: True)

        db = self._db_with(
            morning={
                'id': 999, 'report_type': 'morning',
                'content': 'Operator entered at $50000 with stop $48000.',
                'summary': 'Entered at $50000 with stop $48000 — active long position.',
                'audio_script': 'Active long position entered at $50000.',
                'created_at': datetime.utcnow() - timedelta(hours=1),
            },
            general={
                'id': 111, 'report_type': 'general',
                'content': 'Broad market overview, no personal positions.',
                'summary': 'Markets consolidating near highs.',
                'audio_script': 'Markets are consolidating near highs.',
                'created_at': datetime.utcnow() - timedelta(hours=1),
            },
        )
        monkeypatch.setattr(cdb, 'get_content_db', lambda: db)

        fn = TOOL_REGISTRY['report_get_compact']['handler']
        out = fn({'type': 'morning'})

        assert out['id'] == 111, (
            'Non-admin caller must be served the general report row, '
            'never the operator morning row'
        )
        assert out['summary'] == 'Markets consolidating near highs.'
        assert '50000' not in (out.get('summary') or '')
        assert 'position' not in (out.get('summary') or '').lower()
        assert out['requested_type'] == 'morning'
        assert out['is_fallback'] is True

    def test_admin_morning_request_gets_operator_report(self, monkeypatch):
        """Control case: the admin (operator) is still entitled to the
        real personal 'morning' report when it's fresh — the gate must
        only apply to non-admin callers."""
        from datetime import datetime, timedelta
        from carrac import content_db as cdb
        import carrac.mcp.tools.reports as reports_mod

        monkeypatch.setattr(reports_mod, '_caller_is_admin', lambda: True)
        monkeypatch.setattr(reports_mod, '_caller_is_free', lambda: False)

        db = self._db_with(
            morning={
                'id': 999, 'report_type': 'morning',
                'content': 'Operator entered at $50000.',
                'summary': 'Entered at $50000 — active long position.',
                'audio_script': 'Active long position entered at $50000.',
                'created_at': datetime.utcnow() - timedelta(hours=1),
            },
        )
        monkeypatch.setattr(cdb, 'get_content_db', lambda: db)

        fn = TOOL_REGISTRY['report_get_compact']['handler']
        out = fn({'type': 'morning'})
        assert out['id'] == 999
        assert out['is_fallback'] is False
        # Admin sees their own unsanitized content.
        assert '50000' in out['summary']

    def test_non_admin_existence_fallback_is_sanitized(self, monkeypatch):
        """Defense-in-depth: if 'general' has no row at all yet and the
        (non-free, non-admin) caller's request existence-falls-back to the
        operator 'morning' row, the served summary/audio_script must be
        run through the operator-neutral sanitizer — compact_report() alone
        does not strip position language, so report_get_compact must not rely on
        it as the only safeguard."""
        from datetime import datetime, timedelta
        from carrac import content_db as cdb
        import carrac.mcp.tools.reports as reports_mod

        monkeypatch.setattr(reports_mod, '_caller_is_admin', lambda: False)
        monkeypatch.setattr(reports_mod, '_caller_is_free', lambda: False)

        db = self._db_with(
            morning={
                'id': 777, 'report_type': 'morning',
                'content': 'STORY\nEntered at $50000 with stop $48000.',
                'summary': 'Entered at $50000 with stop $48000, active long position.',
                'audio_script': 'Active long position entered at $50000.',
                'created_at': datetime.utcnow() - timedelta(hours=1),
            },
            general=None,
        )
        monkeypatch.setattr(cdb, 'get_content_db', lambda: db)

        fn = TOOL_REGISTRY['report_get_compact']['handler']
        out = fn({'type': 'morning'})

        assert out['id'] == 777  # existence-fallback landed on the morning row
        # But the operator-private position language must be scrubbed.
        assert '50000' not in (out.get('summary') or '')
        assert '50000' not in (out.get('audio_script') or '')
        assert 'position' not in (out.get('summary') or '').lower()

    def test_weekly_request_is_not_resolved_to_general(self, monkeypatch):
        """'weekly' is a shared report type — _resolve_report_type_for_caller
        must pass it through unchanged for everyone (only 'morning' remaps)."""
        from datetime import datetime, timedelta
        from carrac import content_db as cdb
        import carrac.mcp.tools.reports as reports_mod

        monkeypatch.setattr(reports_mod, '_caller_is_admin', lambda: False)
        monkeypatch.setattr(reports_mod, '_caller_is_free', lambda: True)

        rows = {'weekly': {
            'id': 42, 'report_type': 'weekly',
            'content': 'Weekly digest.', 'summary': 'Week in review.',
            'audio_script': 'Week in review.',
            'created_at': datetime.utcnow() - timedelta(hours=2),
        }}

        class FakeDB:
            def get_latest_report(self, report_type=None):
                return rows.get(report_type)
        monkeypatch.setattr(cdb, 'get_content_db', lambda: FakeDB())

        fn = TOOL_REGISTRY['report_get_compact']['handler']
        out = fn({'type': 'weekly'})
        assert out['id'] == 42
        assert out['requested_type'] == 'weekly'
        assert out['is_fallback'] is False

    def test_unknown_type_raises(self):
        fn = TOOL_REGISTRY['report_get_compact']['handler']
        with pytest.raises(ToolError):
            fn({'type': 'made_up'})

    def test_no_report_found_raises(self, monkeypatch):
        from carrac import content_db as cdb
        import carrac.mcp.tools.reports as reports_mod

        monkeypatch.setattr(reports_mod, '_caller_is_admin', lambda: False)
        monkeypatch.setattr(reports_mod, '_caller_is_free', lambda: True)

        class FakeDB:
            def get_latest_report(self, report_type=None):
                return None
        monkeypatch.setattr(cdb, 'get_content_db', lambda: FakeDB())

        fn = TOOL_REGISTRY['report_get_compact']['handler']
        with pytest.raises(ToolError):
            fn({'type': 'morning'})


class TestGenerateReportOnCondition:
    def test_registered(self):
        assert 'report_generate_conditional' in TOOL_REGISTRY
        assert TOOL_REGISTRY['report_generate_conditional']['min_tier'] == 'premium'

    def test_missing_condition_raises(self):
        fn = TOOL_REGISTRY['report_generate_conditional']['handler']
        with pytest.raises(ToolError):
            fn({})

    def test_returns_brief_with_label(self, monkeypatch):
        from carrac import caches as caches_mod
        monkeypatch.setattr(
            caches_mod, 'get_cached_scanner',
            lambda: {'results': [
                {'symbol': 'BTC', 'price': 50000, 'best_verdict': 'STRONG',
                 'long': {'total': 80}, 'short': {'total': 20}},
            ], 'macro': {'risk': 'risk_on'}},
        )
        fn = TOOL_REGISTRY['report_generate_conditional']['handler']
        out = fn({'condition': 'regime flipped to distribution_topping'})
        assert out['condition'] == 'regime flipped to distribution_topping'
        assert 'BTC' in out['assets']
        assert isinstance(out['scanner'], list)
