"""MCP prompts + resources — the "carrac expertise" for external LLMs.

When a user connects their own AI via MCP, the in-app chat's system-prompt IP
(indicator glossary, quant-engine interpretation guide, anti-hallucination
rules, the non-advice posture, validated trade workflows) is otherwise lost —
the external model only sees tool names + descriptions. This module exposes
that IP as MCP **resources** (readable reference docs) and **prompts**
(structured workflows), so a bring-your-own-AI client reasons with the same
discipline as the in-app Copilot.

Everything here IMPORTS from the canonical sources (carrac.ai.features
BASE_PROMPT / FEATURES, carrac.grounding.preamble, carrac.ai.copy) and is
computed at read time, so there is a single source of truth and no drift.
"""

import logging

from carrac.ai.copy import with_advice_ban
from carrac.mcp.registry import mcp_prompt, mcp_resource

logger = logging.getLogger('carrac.mcp.primitives')


# ── Helpers ─────────────────────────────────────────────────────

def _base_section(start_marker, end_marker=None):
    """Extract a labelled section out of the in-app BASE_PROMPT by header
    substring, so glossary resources track the canonical definitions."""
    from carrac.ai.features import BASE_PROMPT
    i = BASE_PROMPT.find(start_marker)
    if i == -1:
        return ''
    if end_marker:
        j = BASE_PROMPT.find(end_marker, i + len(start_marker))
        if j == -1:
            j = len(BASE_PROMPT)
    else:
        j = len(BASE_PROMPT)
    return BASE_PROMPT[i:j].strip()


# ── Resources ───────────────────────────────────────────────────

@mcp_resource('carrac://glossary/indicators', 'Indicator glossary',
              'How to read every Carrac indicator: Consensio, EMA Ribbon, CPR, '
              'Structure, Bollinger Bands, ADX, Volume, '
              'Weekly RSI, Key Levels, position drawings.')
def _res_indicators():
    return _base_section('INDICATOR GLOSSARY', 'QUANT ENGINE')


@mcp_resource('carrac://glossary/quant-engine', 'Quant engine interpretation guide',
              'How to interpret the cross-asset quant engine: metric domains, '
              'z-scores, FDR-adjusted correlations, lead-lag projected-move + CI, '
              '2-state HMM regime + cycle positioning, anomaly clusters, '
              'transmission chains and their strength bands.')
def _res_quant():
    return _base_section('QUANT ENGINE', 'FORMULA ENGINE')


@mcp_resource('carrac://glossary/formula-dsl', 'Formula engine DSL reference',
              'The custom-formula/alert DSL: operators, quant primitives '
              '(leadlag, chain, regime_prob, anomaly_active, analogue_similarity, '
              'zscore), and worked example expressions.')
def _res_formula_dsl():
    section = _base_section('FORMULA ENGINE', 'ALERT TYPES')
    # Append the live, always-current DSL reference (function registry + limits)
    # from the indicator_get_dsl_reference tool so premium formula authors get exact grammar.
    try:
        from carrac.mcp.tools.indicators import indicator_get_dsl_reference
        live = indicator_get_dsl_reference({})
        import json
        section += '\n\nLIVE DSL REFERENCE (indicator_get_dsl_reference):\n' + json.dumps(live, indent=2, default=str)
    except Exception as e:
        logger.debug('formula-dsl live reference unavailable: %s', e)
    return section


@mcp_resource('carrac://glossary/alert-types', 'Alert-type taxonomy',
              'The alert kinds Carrac supports (price, indicator, trendline, '
              'formula, zscore, regime_transition, chain_strength_breach, '
              'anomaly_cluster_active, conditional_query_match), their tiers, '
              'and the edge-detection + notification-channel rules.')
def _res_alert_types():
    return _base_section('ALERT TYPES', 'BACKTEST MODELS')


@mcp_resource('carrac://glossary/backtest-models', 'Backtest models',
              'The THREE distinct backtest models (Full Simulation, Hold-N '
              'Signal Test, Historical Analog), what each reports, and the rule '
              'never to compare a Hold-N hit rate against a Full-Sim win rate.')
def _res_backtest_models():
    return _base_section('BACKTEST MODELS', None)


@mcp_resource('carrac://grounding/rules', 'Grounding + non-advice rules',
              'The hard anti-hallucination rules (use only data in tool results / '
              'the facts resource, never invent macro numbers), the prompt-injection '
              'posture for user-supplied text, and the MiCA non-advice phrasing '
              'rules. Read this before producing any analysis for a user.')
def _res_grounding_rules():
    # Rule 7 of the preamble is the non-advice ban (MiCA Art. 82 / national
    # regulators) — the same text every in-app prompt carries.
    from carrac.grounding.preamble import render_preamble
    return render_preamble(
        '(Live ground-truth facts are in the carrac://facts/current resource — '
        'read it and treat it as authoritative for macro values and whether an '
        'event has occurred.)'
    )


@mcp_resource('carrac://guide/getting-started', 'Getting started with Carrac over MCP',
              'How to use Carrac from your own AI: the recommended first call, what '
              'tools / prompts / resources exist, and the grounding + non-advice rules. '
              'Read this once at the start of a session.')
def _res_getting_started():
    return (
        "# Getting started with Carrac over MCP\n\n"
        "Carrac serves market data + trading-analysis tools; your AI does the reasoning. "
        "Carrac never gives advice — it gives you the numbers to evaluate.\n\n"
        "## First call\n"
        "Call the **get_started** tool. It returns a compact orientation: today's market "
        "overview, the latest daily report summary, and (with a per-user API key) your own "
        "positions / active plays / watchlist — plus pointers to everything below.\n\n"
        "## Read these resources for the vocabulary + rules\n"
        "- `carrac://glossary/indicators` — how to read every indicator (Consensio, EMA "
        "Ribbon, CPR, Structure, BB, ADX, Volume, Weekly RSI, Key Levels).\n"
        "- `carrac://glossary/quant-engine` — z-scores, FDR-q correlations, lead-lag, HMM "
        "regime, anomaly clusters, transmission chains.\n"
        "- `carrac://glossary/formula-dsl`, `carrac://glossary/alert-types`, "
        "`carrac://glossary/backtest-models` — formula grammar, alert kinds, and the three "
        "distinct backtest models (never compare a Hold-N hit rate to a Full-Sim win rate).\n"
        "- `carrac://grounding/rules` — the hard anti-hallucination rules + the MiCA "
        "non-advice posture. **Read this before producing any analysis.**\n"
        "- `carrac://facts/current` — live ground-truth macro facts. Use these instead of "
        "training memory for Fed funds, FOMC dates, yields, CPI, DXY, BTC, SPX.\n\n"
        "## Ready-made workflows (prompts)\n"
        "- **analyze-asset** — a grounded read of one asset (snapshot + detail + glossary).\n"
        "- **validate-trade** — score your own drawn trade plan with Carrac's scorecard discipline.\n"
        "- **design-trade** — stress-test your thesis against a dual-direction scorecard.\n\n"
        "## Useful free tools\n"
        "market_get_chart / market_get_detail (one asset), market_get_overview / scanner_get_screen (the "
        "board), market_search_symbol, report_get_raw (the daily report), plus your own "
        "journal (trade_log, trade_list, trade_close), plays, drawings, and watchlist.\n\n"
        "## The one rule\n"
        "Ground every claim in a tool result or `carrac://facts/current`. If a number isn't "
        "in the data, say so — don't invent it."
    )


@mcp_resource('carrac://facts/current', 'Live market facts (ground truth)',
              'Freshly-fetched authoritative facts: today\'s date, Fed funds rate + '
              'target range, next/last FOMC, 2Y/10Y yields + spread, CPI YoY, DXY, '
              'BTC, S&P 500. Use these instead of training memory for macro values. '
              'Dynamic — re-read for the latest values (TTLs: prices ~5min, macro ~1h).')
def _res_facts_current():
    from carrac.grounding.facts_block import facts_block
    return facts_block(include_prices=True)


# ── Prompts ─────────────────────────────────────────────────────

_ASSET_ARG = [{'name': 'asset', 'description': 'Ticker symbol, e.g. BTC, ETH, SPX, TSLA.', 'required': True}]


def _feature_instruction(key):
    """Pull a feature's in-app system instruction (DRY — single source)."""
    from carrac.ai.features import FEATURES
    return FEATURES.get(key, {}).get('system_instruction', '')


@mcp_prompt('validate-trade',
            'Evaluate the user\'s own trade plan with Carrac\'s structured 0/1/2 scorecard '
            'discipline (conservative scoring, veto conditions, position-aware R:R). Gathers '
            'context from Carrac tools, then scores honestly. Assesses the plan; never originates one.',
            arguments=_ASSET_ARG)
def _prompt_validate_trade(args):
    asset = (args.get('asset') or '<asset>').strip()
    instr = _feature_instruction('validate')
    text = (
        f"You are connected to Carrac (a market-data platform) via MCP. "
        f"Validate the user's trade idea for {asset}.\n\n"
        f"STEP 1 — Gather real data first (do not analyse from memory). Call:\n"
        f"  • market_get_detail with symbol={asset} (full indicator picture + key levels)\n"
        f"  • trade_get_positions (read the user's actual entry / SL / TP drawing for {asset} if any)\n"
        f"  • play_get_digest (any active thesis on {asset})\n"
        f"STEP 2 — Read carrac://glossary/indicators (definitions) and "
        f"carrac://grounding/rules (hard rules + non-advice posture). Ground every claim "
        f"in the tool results; if a number isn't there, say so — never invent it.\n\n"
        f"STEP 3 — Apply this scoring discipline exactly:\n\n{instr}"
    )
    return [{'role': 'user', 'content': {'type': 'text', 'text': with_advice_ban(text)}}]


@mcp_prompt('design-trade',
            'Stress-test the user\'s thesis on an asset with Carrac\'s dual-direction '
            'scorecard (evidence per direction, thesis validation, explicit invalidation). '
            'Pulls live data from Carrac tools first. Analyses the thesis; never originates '
            'an entry, stop, target or size.',
            arguments=_ASSET_ARG)
def _prompt_design_trade(args):
    asset = (args.get('asset') or '<asset>').strip()
    instr = _feature_instruction('trade_design')
    text = (
        f"You are connected to Carrac via MCP. Stress-test the user's thesis for {asset}.\n\n"
        f"STEP 1 — Gather data: call market_get_detail (symbol={asset}), market_get_chart ({asset}), "
        f"and play_get_digest. For cross-asset/macro context, consider market_get_overview "
        f"and the quant tools.\n"
        f"STEP 2 — Read carrac://glossary/indicators + carrac://glossary/quant-engine for "
        f"vocabulary, and carrac://grounding/rules for the non-advice posture. Ground every "
        f"number in tool results.\n\n"
        f"STEP 3 — Follow this discipline:\n\n{instr}"
    )
    return [{'role': 'user', 'content': {'type': 'text', 'text': with_advice_ban(text)}}]


@mcp_prompt('analyze-asset',
            'Bootstrap a grounded read of one asset: pulls the Carrac snapshot + full '
            'detail, applies the indicator glossary, and structures the analysis '
            '(trend, key levels, indicators, confluences/divergences).',
            arguments=_ASSET_ARG)
def _prompt_analyze_asset(args):
    asset = (args.get('asset') or '<asset>').strip()
    instr = _feature_instruction('viewport')
    text = (
        f"You are connected to Carrac via MCP. Produce a grounded read of {asset}.\n\n"
        f"STEP 1 — Call market_get_chart (symbol={asset}) for a fast snapshot, then market_get_detail "
        f"(symbol={asset}) for the full multi-timeframe indicator picture + key levels. "
        f"Optionally market_get_overview for context.\n"
        f"STEP 2 — Interpret strictly using carrac://glossary/indicators (and "
        f"carrac://glossary/quant-engine if quant data is present). Follow "
        f"carrac://grounding/rules — every claim backed by a number from the tools.\n\n"
        f"STEP 3 — {instr}"
    )
    return [{'role': 'user', 'content': {'type': 'text', 'text': with_advice_ban(text)}}]
