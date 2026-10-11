"""Tests for the free-tier shared "general" report.

Covers two surfaces:
  1. Engine — `_generate_general_report` builds an operator-NEUTRAL briefing
     (no alerts/positions/yesterday) and saves report_type="general".
  2. MCP    — free callers transparently resolve morning -> general; the
     `general` type is exposed in the tool enums.

Pure-logic + in-process Flask tests — no live API, no LLM.

A third surface — Web, `/api/report/morning/latest` serving general to free
vs morning to paid — is no longer tested here via the Flask test client
(TestWebServingTierSplit, TestWebFreeTierMorningFallbackClosed). That route
is FastAPI-migrated (strangler MIGRATED_PATHS); the Flask handler was dead
code, shadowed in production, so those suites were deleted. Live web
coverage: tests/fastapi/test_reports_gating.py + test_reports_free_tier.py
(both drive the real handler via asgi_client / reports_client).
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _grounding_verifier_off(monkeypatch):
    """Generating a report runs the Haiku grounding verifier, which
    builds its OWN Anthropic client when none is injected
    (carrac/grounding/verify.py::verify_against_source) — a real, billed API call on any box
    where ANTHROPIC_API_KEY is set. Nothing in this file is about the verifier
    (tests/test_verify_wiring.py covers it), so use the product's own kill
    switch rather than a mock: verify_against_source short-circuits to CLEAN.
    """
    monkeypatch.setenv("GROUNDING_VERIFY_ENABLED", "0")


# ----------------------------------------------------------------------
# 1. Engine — operator-neutral generation
# ----------------------------------------------------------------------

class _FakeSonnet:
    """Captures the user_message handed to the LLM."""
    def __init__(self):
        self.captured = {}

    def chat(self, system_prompt=None, user_message=None, **kw):
        self.captured['system_prompt'] = system_prompt
        self.captured['user_message'] = user_message
        return {'content': '## The Story\nBTC is steady.\n\n## Market Snapshot\n| Asset | Price |\n| BTC | 100 |\n'}


class TestGeneralReportGeneration:
    def _shared_parts(self):
        return [
            '## Macro Context\nFed on hold; DXY soft.',
            '## Pre-Processed Signals\n{"BTC": {"rsi": 51}}',
        ]

    def test_general_report_saved_with_clean_briefing(self, monkeypatch):
        import morning_engine.engine as eng

        posted = {}

        def fake_post(content, report_type=None, assets=None,
                      generation_time_ms=0, **kw):
            posted['report_type'] = report_type
            posted['content'] = content
            posted['assets'] = assets
            return 777

        monkeypatch.setattr(eng, '_post_report', fake_post)
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)

        sonnet = _FakeSonnet()
        rid = eng._generate_general_report(
            shared_briefing_parts=self._shared_parts(),
            spotlight_symbols=['BTC', 'ETH'],
            compact_summary_text='SPX flat, GOLD +0.4%',
            steering='', patterns='',
            sonnet=sonnet, t_start=time.time(),
        )

        assert rid == 777
        assert posted['report_type'] == 'general'

        um = sonnet.captured['user_message']
        # CRITICAL: no operator-private context leaks into the public free report.
        assert '## Alerts' not in um
        assert '## Positions' not in um
        assert '## Yesterday' not in um
        # Reuses the shared macro/signal parts + spotlight + compact summary.
        assert 'Macro Context' in um
        assert 'Pre-Processed Signals' in um
        assert 'SPOTLIGHT ASSETS: BTC, ETH' in um
        assert 'SPX flat, GOLD +0.4%' in um

    def test_general_report_disabled_flag_short_circuits(self, monkeypatch):
        import morning_engine.engine as eng

        called = {'posted': False}
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', False)
        monkeypatch.setattr(
            eng, '_post_report',
            lambda *a, **k: called.__setitem__('posted', True) or 1,
        )

        rid = eng._generate_general_report(
            shared_briefing_parts=[], spotlight_symbols=[],
            compact_summary_text='', steering='', patterns='',
            sonnet=_FakeSonnet(), t_start=time.time(),
        )
        assert rid is None
        assert called['posted'] is False

    def test_general_report_failure_is_non_fatal(self, monkeypatch):
        import morning_engine.engine as eng

        class Boom:
            def chat(self, **kw):
                raise RuntimeError('LLM down')

        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)
        # Should swallow the exception and return None, never raise.
        rid = eng._generate_general_report(
            shared_briefing_parts=self._shared_parts(),
            spotlight_symbols=['BTC'], compact_summary_text='',
            steering='', patterns='', sonnet=Boom(), t_start=time.time(),
        )
        assert rid is None


# ----------------------------------------------------------------------
# 2. MCP — caller-aware type resolution + enum exposure
# ----------------------------------------------------------------------

class TestMcpReportTypeResolution:
    def test_free_morning_resolves_to_general(self, monkeypatch):
        import carrac.mcp.runtime as rt
        import carrac.auth.decorators as dec
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'free')
        monkeypatch.setattr(dec, 'get_tier_limits', lambda t: {'personal_report': 'none'})
        from carrac.mcp.tools.reports import _resolve_report_type_for_caller
        assert _resolve_report_type_for_caller('morning') == 'general'
        # Non-morning types pass through untouched.
        assert _resolve_report_type_for_caller('weekly') == 'weekly'
        assert _resolve_report_type_for_caller('general') == 'general'

    def test_paid_morning_resolves_to_general(self, monkeypatch):
        # The operator report is admin-only: paid tiers resolve to general too
        # (position-tracking reports return as a per-user paid feature).
        import carrac.mcp.runtime as rt
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'premium')
        from carrac.mcp.tools.reports import _resolve_report_type_for_caller
        assert _resolve_report_type_for_caller('morning') == 'general'

    def test_admin_morning_stays_morning(self, monkeypatch):
        import carrac.mcp.runtime as rt
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'admin')
        from carrac.mcp.tools.reports import _resolve_report_type_for_caller
        assert _resolve_report_type_for_caller('morning') == 'morning'


class TestMcpReportEnums:
    def test_general_exposed_in_tool_enums(self):
        from carrac.mcp import tools  # noqa: F401 — ensure registration
        from carrac.mcp.registry import TOOL_REGISTRY
        raw = TOOL_REGISTRY['report_get_raw']['inputSchema']
        sect = TOOL_REGISTRY['report_get_section']['inputSchema']
        assert 'general' in raw['properties']['type']['enum']
        assert 'general' in sect['properties']['type']['enum']


# ----------------------------------------------------------------------
# 3. Web — the /api/report/general SAVE route
# ----------------------------------------------------------------------

class TestGeneralReportSaveRoute:
    """Pins that post_report(report_type='general') targets /api/report/general.

    The engine test above mocks _post_report and the serving test seeds the DB
    directly, so neither exercised the real save route
    drives the actual route end-to-end.
    """
    def test_post_general_report_persists_and_serves(self, client, app):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'admin@example.com'
            sess['tier'] = 'premium'
            sess['is_admin'] = True  # save routes now require admin-or-internal-key
        resp = client.post('/api/report/general', json={
            'content': '## The Story\nGENERAL-ROUTE-OK\n\n## Market Snapshot\n| BTC | 100 |',
            'assets': ['BTC'],
            'generation_time_ms': 1234,
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert (resp.get_json() or {}).get('report_id') is not None

        from carrac.content_db import get_content_db
        row = get_content_db().get_latest_report(report_type='general')
        assert row and 'GENERAL-ROUTE-OK' in row['content']

    def test_general_route_rejects_unauthenticated(self, client):
        resp = client.post('/api/report/general', json={'content': 'x'})
        assert resp.status_code == 401


# ----------------------------------------------------------------------
# 5. Morning-report notification dedup (operator gets 1 ping/day, not 5)
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# 6. Defense-in-depth: a free tier must NEVER receive the operator's
#    positions/alerts. The free→general resolution falls back to the
#    operator 'morning' report when no general row exists yet — that
#    fallback is the leak path. Close it + sanitize as a backstop.
# ----------------------------------------------------------------------

# A shared 'morning' report: The Story + Market Snapshot are operator-neutral,
# but Action Dashboard bakes in the operator's real positions (see prompts.py
# — "The Story owns ... position-level context"; Action Dashboard is the
# position checklist). This is exactly what must not reach a free account.
_MORNING_WITH_POSITIONS = (
    "## The Story\n"
    "Macro narrative; nothing operator-private here.\n\n"
    "## Action Dashboard\n"
    "\U0001f534 **BTC Position Risk** — Short from $75,736 is +6.2% in-the-money.\n"
    "\U0001f534 **XRP Short Cluster** — three shorts, entries $1.39, $1.34, $1.35.\n\n"
    "## Market Snapshot\n"
    "| Asset | Price |\n| BTC | 71287 |\n"
)


class TestFreeTierSanitizer:
    """`strip_operator_sections` removes operator-private sections (positions /
    alerts) from a shared report so they can never reach a free consumer."""

    def test_strips_action_dashboard_keeps_neutral_sections(self):
        from carrac.reports.sanitize import strip_operator_sections
        out = strip_operator_sections(_MORNING_WITH_POSITIONS)
        assert 'Action Dashboard' not in out
        assert '75,736' not in out
        assert 'XRP Short Cluster' not in out
        # Operator-neutral sections survive untouched.
        assert '## The Story' in out
        assert '## Market Snapshot' in out
        assert '71287' in out

    def test_strips_action_items_and_alerts_positions_followup(self):
        from carrac.reports.sanitize import strip_operator_sections
        text = (
            "## The Story\nx\n\n"
            "## Action Items\n- close BTC short\n\n"
            "## Alerts & Positions Follow-Up\n- ETH long underwater\n\n"
            "## Market Snapshot\n| BTC | 1 |\n"
        )
        out = strip_operator_sections(text)
        assert 'Action Items' not in out
        assert 'Alerts & Positions Follow-Up' not in out
        assert 'close BTC short' not in out
        assert 'ETH long underwater' not in out
        assert '## The Story' in out
        assert '## Market Snapshot' in out

    def test_noop_on_general_report(self):
        from carrac.reports.sanitize import strip_operator_sections
        general = "## The Story\nNeutral macro.\n\n## Market Snapshot\n| BTC | 1 |\n"
        assert strip_operator_sections(general) == general

    def test_handles_empty_and_none(self):
        from carrac.reports.sanitize import strip_operator_sections
        assert strip_operator_sections('') == ''
        assert strip_operator_sections(None) is None


class TestMcpFreeTierMorningFallbackClosed:
    """MCP `report_get_raw` / `report_get_section`: a free caller
    whose general report doesn't exist yet must not fall back to the operator
    'morning' report."""

    def _only_morning_db(self):
        class FakeDB:
            def get_latest_report(self, report_type=None):
                if report_type == 'morning':
                    return {
                        'id': 9, 'report_type': 'morning',
                        'content': _MORNING_WITH_POSITIONS,
                        'html': _MORNING_WITH_POSITIONS,
                        'summary': 's', 'assets_included': [],
                        'created_at': None,
                    }
                return None  # no general report exists
        return FakeDB()

    def _set_free(self, monkeypatch):
        import carrac.mcp.runtime as rt
        import carrac.auth.decorators as dec
        from carrac import content_db as cdb
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'free')
        monkeypatch.setattr(dec, 'get_tier_limits', lambda t: {'personal_report': 'none'})
        monkeypatch.setattr(cdb, 'get_content_db', lambda: self._only_morning_db())

    def test_raw_free_caller_does_not_fall_back_to_morning(self, monkeypatch):
        from carrac.mcp import tools  # noqa: F401 — ensure tool registration
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.registry import TOOL_REGISTRY
        self._set_free(monkeypatch)
        fn = TOOL_REGISTRY['report_get_raw']['handler']
        with pytest.raises(ToolError):
            fn({'type': 'morning'})

    def test_section_free_caller_cannot_pull_action_dashboard(self, monkeypatch):
        from carrac.mcp import tools  # noqa: F401 — ensure tool registration
        from carrac.mcp.protocol import ToolError
        from carrac.mcp.registry import TOOL_REGISTRY
        self._set_free(monkeypatch)
        fn = TOOL_REGISTRY['report_get_section']['handler']
        with pytest.raises(ToolError):
            fn({'section': 'Action Dashboard', 'type': 'morning'})

    def test_raw_paid_caller_gets_neutralized_fallback(self, monkeypatch):
        # Paid caller, no general row: the morning report may serve as the
        # fallback BASE, but only through the operator-neutral filter — the
        # operator's book never reaches a non-admin caller.
        import carrac.mcp.runtime as rt
        import carrac.auth.decorators as dec
        from carrac import content_db as cdb
        from carrac.mcp import tools  # noqa: F401 — ensure tool registration
        from carrac.mcp.registry import TOOL_REGISTRY
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'premium')
        monkeypatch.setattr(dec, 'get_tier_limits', lambda t: {'personal_report': 'full'})
        monkeypatch.setattr(cdb, 'get_content_db', lambda: self._only_morning_db())
        fn = TOOL_REGISTRY['report_get_raw']['handler']
        out = fn({'type': 'morning'})
        content = out['report']['content']
        assert content.strip(), 'paid caller must still receive content'
        assert 'Action Dashboard' not in content
        assert '75,736' not in content

    def test_raw_admin_caller_gets_full_morning(self, monkeypatch):
        import carrac.mcp.runtime as rt
        from carrac import content_db as cdb
        from carrac.mcp import tools  # noqa: F401 — ensure tool registration
        from carrac.mcp.registry import TOOL_REGISTRY
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'admin')
        monkeypatch.setattr(cdb, 'get_content_db', lambda: self._only_morning_db())
        fn = TOOL_REGISTRY['report_get_raw']['handler']
        out = fn({'type': 'morning'})
        assert 'Action Dashboard' in out['report']['content']
        assert '75,736' in out['report']['content']


class TestMorningReportNotifyDedup:
    """The 5 regional dispatches each re-save the shared morning report; the
    operator must be pinged only on the FIRST save of the UTC day, not every
    dispatch (was 5x ntfy + 5x full-report Telegram)."""

    def test_second_same_day_save_does_not_re_notify(self, client, app, monkeypatch):
        import carrac.reports.routes as rr
        n = {'tg': 0}
        monkeypatch.setattr(rr, 'send_telegram_message',
                            lambda *a, **k: n.__setitem__('tg', n['tg'] + 1))
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'admin@example.com'
            # This route now requires an ADMIN session (or the
            # engine key), not merely a logged-in one. The session here was
            # already labelled admin@example.com and clearly meant to be the
            # operator; it just never set the flag, because until now the route
            # did not look at it. This test is about notification dedup, not
            # auth — TestSaveRoutesAdminOnlyFallback owns the auth assertions.
            sess['is_admin'] = True
            sess['tier'] = 'admin'
        payload = {'content': '## The Story\nDedup test', 'assets': ['BTC']}
        assert client.post('/api/report/morning', json=payload).status_code == 200
        after_first = n['tg']
        assert client.post('/api/report/morning', json=payload).status_code == 200
        # A later same-day dispatch re-saves the report but must add NO new ping.
        assert n['tg'] == after_first, 'second same-day morning save re-notified'


# ----------------------------------------------------------------------
# 6. Afternoon report — operator-neutral for non-admin (demo-leak pin)
# ----------------------------------------------------------------------

_AFTERNOON_WITH_POSITIONS = (
    "## 1. Session Summary\nRisk-off into the close; breadth 27%.\n\n"
    "## 5. Updated Action Items\n"
    "- BTC short entered at $75,736 — trail the stop.\n"
    "- Four XRP shorts deep in-the-money.\n\n"
    "## 6. Overnight Watch (Asia Session)\nWatch the 61k shelf.\n"
)


class TestAfternoonOperatorNeutral:
    """`/api/report/afternoon/latest` had NO gating — any keyed user received
    the operator's Updated Action Items (live entries/stops). Non-admin now
    gets the operator-neutral cut; the operator keeps the full report."""

    def _seed(self):
        from carrac.content_db import get_content_db
        get_content_db().save_report(
            content=_AFTERNOON_WITH_POSITIONS, assets_included=[],
            generation_time_ms=0, report_type='afternoon')

    def test_premium_gets_neutralized_afternoon(self, client, app):
        self._seed()
        with client.session_transaction() as sess:
            sess['user_id'] = 41
            sess['email'] = 'premium-a@example.com'
            sess['tier'] = 'premium'
        resp = client.get('/api/report/afternoon/latest')
        assert resp.status_code == 200
        content = (resp.get_json() or {}).get('content') or ''
        # numbered section heading is stripped despite the "5." prefix
        assert 'Updated Action Items' not in content
        assert '75,736' not in content
        assert 'in-the-money' not in content
        # neutral market prose survives
        assert 'Session Summary' in content
        assert 'Overnight Watch' in content

    def test_admin_gets_full_afternoon(self, client, app):
        self._seed()
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'operator@example.com'
            sess['tier'] = 'admin'
        resp = client.get('/api/report/afternoon/latest')
        assert resp.status_code == 200
        content = (resp.get_json() or {}).get('content') or ''
        assert 'Updated Action Items' in content
        assert '75,736' in content


# ----------------------------------------------------------------------
# 7. Sanitizer units — numbered headings + position-line scrub
# ----------------------------------------------------------------------

class TestSanitizerUnits:
    def test_numbered_section_heading_is_stripped(self):
        from carrac.reports.sanitize import strip_operator_sections
        md = ("## 1. Session Summary\nfine\n\n"
              "## 5. Updated Action Items\nsecret entries\n\n"
              "## 6. Overnight Watch\nfine too\n")
        out = strip_operator_sections(md)
        assert 'Updated Action Items' not in out
        assert 'secret entries' not in out
        assert 'Session Summary' in out and 'Overnight Watch' in out

    def test_scrub_drops_trade_fingerprint_lines_keeps_market_prose(self):
        from carrac.reports.sanitize import scrub_position_lines
        md = ("BTC backwardation deepened while breadth stayed weak.\n"
              "🔴 BTC — Active Short Position: short entered at $75,736 with stop $79,872.\n"
              "Funding flat; puts remain expensive across the curve.\n")
        out = scrub_position_lines(md)
        assert 'entered at $75,736' not in out
        assert 'Active Short Position' not in out
        assert 'backwardation deepened' in out
        assert 'Funding flat' in out

    def test_operator_neutral_composes_both_layers(self):
        from carrac.reports.sanitize import operator_neutral
        # NB: the scrub is LINE-level — a fingerprint anywhere drops the whole
        # line, so the position sentence sits on its own line here, exactly as
        # the engine writes bullets/sentences in practice.
        md = ("## The Story\nMacro is heavy.\n"
              "Long ETH entered at $2,251 — underwater.\n\n"
              "## Action Dashboard\n🔴 everything here is private\n\n"
              "## Scenarios\nRange break favors continuation.\n")
        out = operator_neutral(md)
        assert 'Action Dashboard' not in out
        assert 'entered at $2,251' not in out
        assert 'Macro is heavy' in out
        assert 'Range break favors continuation' in out


# ----------------------------------------------------------------------
# 8. Weekly / week-ahead — operator-neutral for non-admin (were ungated)
# ----------------------------------------------------------------------

_WEEKLY_WITH_PLAYS = (
    "## 1. Week in Review\nBTC opened the week at $73,413 and closed at $60,786.\n\n"
    "## 4. What Worked / What Didn't\n"
    "- The OIL $95-break play stopped out; sizing was too eager.\n\n"
    "## 5. Play Review\n"
    "- Play #18 OIL Supply-Shock: thesis intact, re-armed.\n\n"
    "## 7. Next Week Setup\nWatch the 59.5k shelf and FOMC.\n"
)


class TestWeeklyOperatorNeutral:
    """`/api/report/weekly/latest` + `week_ahead/latest` had NO gating — any
    keyed user received the operator's weekly digest (Play Review, What
    Worked / What Didn't = the operator's own trading retro)."""

    def _seed(self, rtype):
        from carrac.content_db import get_content_db
        get_content_db().save_report(
            content=_WEEKLY_WITH_PLAYS, assets_included=[],
            generation_time_ms=0, report_type=rtype)

    def test_premium_weekly_is_neutralized(self, client, app):
        self._seed('weekly')
        with client.session_transaction() as sess:
            sess['user_id'] = 51
            sess['email'] = 'premium-w@example.com'
            sess['tier'] = 'premium'
        resp = client.get('/api/report/weekly/latest')
        assert resp.status_code == 200
        content = (resp.get_json() or {}).get('content') or ''
        assert 'Play Review' not in content
        assert "What Worked / What Didn't" not in content
        assert 'Play #18' not in content
        assert 'sizing was too eager' not in content
        # market sections survive
        assert 'Week in Review' in content
        assert 'Next Week Setup' in content

    def test_admin_weekly_full(self, client, app):
        self._seed('weekly')
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'operator@example.com'
            sess['tier'] = 'admin'
        resp = client.get('/api/report/weekly/latest')
        assert resp.status_code == 200
        content = (resp.get_json() or {}).get('content') or ''
        assert 'Play Review' in content
        assert 'Play #18' in content

    def test_premium_week_ahead_neutralized_same_path(self, client, app):
        self._seed('week_ahead')
        with client.session_transaction() as sess:
            sess['user_id'] = 52
            sess['email'] = 'premium-wa@example.com'
            sess['tier'] = 'premium'
        resp = client.get('/api/report/week_ahead/latest')
        assert resp.status_code == 200
        content = (resp.get_json() or {}).get('content') or ''
        assert 'Play Review' not in content
        assert 'Week in Review' in content

    def test_mcp_raw_premium_weekly_is_neutralized(self, monkeypatch):
        import carrac.mcp.runtime as rt
        import carrac.auth.decorators as dec
        from carrac import content_db as cdb
        from carrac.mcp import tools  # noqa: F401 — ensure tool registration
        from carrac.mcp.registry import TOOL_REGISTRY
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'premium')
        monkeypatch.setattr(dec, 'get_tier_limits',
                            lambda t: {'personal_report': 'full'})

        class _Db:
            def get_latest_report(self, report_type=None):
                if report_type == 'weekly':
                    return {'content': _WEEKLY_WITH_PLAYS,
                            'created_at': '2026-06-07', 'id': 9}
                return None

        monkeypatch.setattr(cdb, 'get_content_db', lambda: _Db())
        fn = TOOL_REGISTRY['report_get_raw']['handler']
        out = fn({'type': 'weekly'})
        c = out['report']['content']
        assert 'Play Review' not in c
        assert 'Play #18' not in c
        assert 'Next Week Setup' in c


class TestSanitizerWeeklySections:
    def test_slash_heading_is_stripped(self):
        from carrac.reports.sanitize import strip_operator_sections
        md = ("## 1. Week in Review\nfine\n\n"
              "## 4. What Worked / What Didn't\nprivate retro\n\n"
              "## 7. Next Week Setup\nfine too\n")
        out = strip_operator_sections(md)
        assert 'What Worked' not in out
        assert 'private retro' not in out
        assert 'Week in Review' in out and 'Next Week Setup' in out
