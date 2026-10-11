"""Tests for MCP prompts + resources primitives.

Carrac's MCP server was tools-only; these add the prompts + resources
primitives that carry the in-app "carrac expertise" (glossary, grounding
rules, validated workflows) to external bring-your-own-AI clients.

Pure-logic — no live API. (carrac://facts/current does a live data lookup but
degrades gracefully; we only assert it doesn't raise.)
"""

from __future__ import annotations


# Ensure tools + primitives are registered.
import carrac.mcp.tools  # noqa: F401
import carrac.mcp.primitives  # noqa: F401
from carrac.mcp.protocol import dispatch
from carrac.mcp.registry import (
    PROMPT_REGISTRY, RESOURCE_REGISTRY,
    read_resource,
)


def _rpc(method, params=None, _id=1):
    return dispatch({'jsonrpc': '2.0', 'id': _id, 'method': method, 'params': params or {}})


# ── Registration ────────────────────────────────────────────────

class TestRegistration:
    def test_prompts_registered(self):
        assert {'validate-trade', 'design-trade', 'analyze-asset'} <= set(PROMPT_REGISTRY)

    def test_resources_registered(self):
        expected = {
            'carrac://glossary/indicators',
            'carrac://glossary/quant-engine',
            'carrac://glossary/formula-dsl',
            'carrac://glossary/alert-types',
            'carrac://glossary/backtest-models',
            'carrac://grounding/rules',
            'carrac://facts/current',
        }
        assert expected <= set(RESOURCE_REGISTRY)

    def test_all_primitives_free_tier(self):
        # The whole point is to help free BYO-AI users — everything is free.
        assert all(p['min_tier'] == 'free' for p in PROMPT_REGISTRY.values())
        assert all(r['min_tier'] == 'free' for r in RESOURCE_REGISTRY.values())


# ── initialize advertises the new capabilities ──────────────────

class TestCapabilities:
    def test_initialize_advertises_prompts_and_resources(self):
        result = _rpc('initialize', {'clientInfo': {'name': 'test'}})['result']
        caps = result['capabilities']
        assert 'tools' in caps and 'prompts' in caps and 'resources' in caps

    def test_instructions_reference_resources(self):
        result = _rpc('initialize')['result']
        instr = result['instructions']
        assert 'carrac://glossary/indicators' in instr
        assert 'carrac://grounding/rules' in instr
        # The non-advice posture must be surfaced.
        assert 'buy' in instr and 'sell' in instr


# ── prompts/list + prompts/get ──────────────────────────────────

class TestPrompts:
    def test_prompts_list(self):
        prompts = _rpc('prompts/list')['result']['prompts']
        names = {p['name'] for p in prompts}
        assert {'validate-trade', 'design-trade', 'analyze-asset'} <= names
        # arguments spec is surfaced
        vt = next(p for p in prompts if p['name'] == 'validate-trade')
        assert any(a['name'] == 'asset' and a['required'] for a in vt['arguments'])

    def test_prompts_get_substitutes_asset_and_names_tools(self):
        result = _rpc('prompts/get', {'name': 'validate-trade', 'arguments': {'asset': 'ETH'}})['result']
        assert result.get('description')
        text = result['messages'][0]['content']['text']
        assert 'ETH' in text
        # Embeds the in-app validate discipline + tells the LLM to call carrac tools.
        assert 'market_get_detail' in text
        assert 'carrac://grounding/rules' in text

    def test_prompts_get_unknown_is_invalid_params(self):
        err = _rpc('prompts/get', {'name': 'does-not-exist'}).get('error', {})
        assert err.get('code') == -32602


# ── resources/list + resources/read ─────────────────────────────

class TestResources:
    def test_resources_list(self):
        resources = _rpc('resources/list')['result']['resources']
        uris = {r['uri'] for r in resources}
        assert 'carrac://glossary/indicators' in uris
        # Each advertised resource has name + mimeType.
        assert all(r.get('name') and r.get('mimeType') for r in resources)

    def test_static_resources_render_nonempty(self):
        for uri in RESOURCE_REGISTRY:
            if uri == 'carrac://facts/current':
                continue  # live lookup — covered separately
            result = _rpc('resources/read', {'uri': uri})['result']
            text = result['contents'][0]['text']
            assert len(text) > 50, f'{uri} rendered empty'
            assert result['contents'][0]['uri'] == uri

    def test_grounding_rules_content(self):
        result = _rpc('resources/read', {'uri': 'carrac://grounding/rules'})['result']
        text = result['contents'][0]['text']
        assert 'HARD RULES' in text
        # MiCA non-advice banned phrases present.
        assert 'buy' in text and 'sell' in text

    def test_glossary_indicators_has_definitions(self):
        result = _rpc('resources/read', {'uri': 'carrac://glossary/indicators'})['result']
        text = result['contents'][0]['text']
        assert 'Consensio' in text and 'CPR' in text

    def test_facts_current_does_not_raise(self):
        # Live data lookup — must degrade gracefully, never raise.
        result = _rpc('resources/read', {'uri': 'carrac://facts/current'})['result']
        assert 'contents' in result

    def test_resources_read_unknown_is_invalid_params(self):
        err = _rpc('resources/read', {'uri': 'carrac://nope/nope'}).get('error', {})
        assert err.get('code') == -32602


# ── DRY: resources track the canonical sources ──────────────────

class TestGetStarted:
    def test_get_started_registered_free(self):
        from carrac.mcp.registry import TOOL_REGISTRY
        assert 'get_started' in TOOL_REGISTRY
        assert TOOL_REGISTRY['get_started']['min_tier'] == 'free'

    def test_get_started_returns_orientation(self):
        from carrac.mcp.tools.onboarding import get_started
        out = get_started({})
        # Always present (market + report are not user-scoped; next_steps is static).
        assert 'market_overview' in out
        assert 'latest_report' in out
        assert out['next_steps']['try_prompts'] == ['analyze-asset', 'validate-trade', 'design-trade']
        # User-scoped sections present (data or a graceful marker — never a hard fail).
        for key in ('positions', 'plays', 'watchlist'):
            assert key in out

    def test_getting_started_resource(self):
        result = _rpc('resources/read', {'uri': 'carrac://guide/getting-started'})['result']
        text = result['contents'][0]['text']
        assert 'get_started' in text
        assert 'carrac://grounding/rules' in text
        assert 'validate-trade' in text

    def test_get_started_stale_morning_reports_fresh_general_not_dead_row(self, monkeypatch):
        """Pins that get_started must never surface a dead 'morning'
        pipeline's decades-old last row as "the latest report". When
        'morning' is stale (its pipeline stalled) and a fresh shared
        'general' report exists, latest_report must reflect the fresh
        general report — with the substitution flagged, not hidden."""
        from datetime import datetime, timedelta
        import carrac.mcp.runtime as rt
        from carrac import content_db as cdb
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'admin')

        rows = {
            'morning': {
                'id': 277, 'report_type': 'morning',
                'content': 'STALE', 'html': 'STALE',
                'summary': 'stale summary', 'assets_included': [],
                'created_at': datetime.utcnow() - timedelta(days=32),
            },
            'general': {
                'id': 350, 'report_type': 'general',
                'content': 'FRESH', 'html': 'FRESH',
                'summary': 'fresh summary', 'assets_included': [],
                'created_at': datetime.utcnow() - timedelta(hours=1),
            },
        }

        class FakeDB:
            def get_latest_report(self, report_type=None):
                return rows.get(report_type)

        monkeypatch.setattr(cdb, 'get_content_db', lambda: FakeDB())

        from carrac.mcp.tools.onboarding import get_started
        out = get_started({})
        lr = out['latest_report']
        assert lr['id'] == 350
        assert lr['type'] == 'general'
        assert lr['summary'] == 'fresh summary'
        assert lr['requested_type'] == 'morning'
        assert lr['is_fallback'] is True
        assert lr['is_stale'] is False


class TestSourceFidelity:
    def test_indicators_resource_matches_base_prompt_section(self):
        # Resource is extracted live from BASE_PROMPT — proves no drift.
        from carrac.ai.features import BASE_PROMPT
        text = read_resource('carrac://glossary/indicators')
        assert 'INDICATOR GLOSSARY' in BASE_PROMPT  # source marker exists
        assert text and text in BASE_PROMPT  # extracted verbatim from source

    def test_backtest_models_warns_against_cross_model_comparison(self):
        text = read_resource('carrac://glossary/backtest-models')
        assert 'Hold-N' in text and 'Full Sim' in text
