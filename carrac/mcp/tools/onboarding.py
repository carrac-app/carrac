"""MCP onboarding tools.

  get_started — one-shot orientation for a fresh BYO-AI session: market context
  + latest daily report + the caller's own positions/plays/watchlist (when a
  per-user key is bound) + pointers to the prompts/resources to use next.
"""

import logging

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError

logger = logging.getLogger('carrac.mcp.tools.onboarding')


def _safe(fn, arguments):
    """Call a tool handler, returning its result or a graceful marker.

    User-scoped tools raise ToolError when the caller used the shared admin key
    (no per-user binding) — surface that as `requires_user_key` instead of
    failing the whole orientation.
    """
    try:
        return fn(arguments)
    except ToolError as e:
        return {'requires_user_key': str(e)}
    except Exception as e:  # never let one slow/failed source break get_started
        return {'error': str(e)}


def _backtesting_hint() -> dict:
    """Name the backtester in get_started's next_steps.

    An agent following Carrac's documented flow (connect → get_started →
    get_guide) must be told the backtester exists, and get_guide('backtest')
    must not dead-end. Backtesting against the scanner's
    own definitions is one of the few things a DIY connector setup can't do, so
    a first-contact tool that omits it hides the differentiator.

    Entitlement-aware, because the tools/list a free caller sees is filtered —
    telling them to call backtest_run would point at a tool they cannot see.
    """
    from carrac.mcp.runtime import get_mcp_caller_tier
    from carrac.auth.decorators import get_tier_limits

    entitled = bool(get_tier_limits(get_mcp_caller_tier()).get('backtester'))
    if not entitled:
        return {
            'available': False,
            'summary': (
                'Backtesting is not in your tool list on this plan. The backtest_* '
                'tools appear once the account is on a paid plan — do not attempt '
                'to call them until then.'
            ),
        }
    return {
        'available': True,
        'summary': (
            'You can test a trading rule against history. Three DIFFERENT models — '
            'read carrac://glossary/backtest-models before reporting any number, and '
            'never compare a Hold-N hit rate to a Full-Sim win rate.'
        ),
        'tools': {
            'backtest_simulate': 'Full simulation — entry + exit formulas, costs, '
                                 'stops. Win rate, Sharpe, drawdown, equity curve.',
            'backtest_run': 'Hold-N signal test — one boolean condition, forward '
                            'return over N bars. Works against a formula, a saved '
                            'play, or the whole scanner universe (source=scan).',
            'backtest_get_historical_analog': 'Forward-return distribution when '
                                              'given macro conditions held before.',
            'backtest_save_run': 'Persist a run so it survives the session.',
            'backtest_get_runs': 'List previously saved runs, or fetch one by id.',
        },
        'suggested_first_call': (
            "backtest_run with source='scan' — tests one condition across every "
            'asset currently matching the scanner filter, ranked by edge. This is '
            'the thing that is hard to reproduce outside Carrac.'
        ),
    }


@mcp_tool(
    'get_started',
    'One-shot orientation for a new Carrac MCP session — call this FIRST when you '
    'connect. Returns a compact snapshot: today\'s market_overview (top assets + '
    'verdicts), the latest_report summary (free tier = the shared general report), '
    'and — when a per-user API key is bound — your open positions, active plays, '
    'and watchlist (each marked requires_user_key if the shared key is used). '
    'FRESHNESS: latest_report never silently surfaces a dead pipeline\'s old '
    'output as current — if the default "morning" report has no recent row, it '
    'falls back to the freshest available shared report (general/afternoon/'
    'weekly). latest_report.requested_type vs .type tell you whether a '
    'substitution happened (e.g. a shared report standing in for the '
    'personalized one); .is_stale/.age_hours tell you whether even what was '
    'returned is old — freshness is reported, never assumed. '
    'Also returns next_steps pointing at the glossary/grounding resources '
    '(resources/list), the validate-trade / design-trade / analyze-asset prompts '
    '(prompts/list), and a backtesting block naming the backtest_* tools and '
    'whether this account can reach them. No side effects; read-only.',
    {'type': 'object', 'properties': {}},
    min_tier='free',
)
def get_started(arguments):
    from carrac.mcp.tools.market import market_get_overview
    from carrac.mcp.tools.reports import report_get_raw
    from carrac.mcp.tools.journal import trade_get_positions
    from carrac.mcp.tools.plays import play_get_digest
    from carrac.mcp.tools.watchlist import watchlist_manage

    out = {}

    # Market context (not user-scoped).
    out['market_overview'] = _safe(market_get_overview, {'limit': 6})

    # Latest daily report — summary only. report_get_raw already
    # applies tier-routing (free -> general) AND a freshness fallback: a
    # stale/absent "morning" row (e.g. the personalized pipeline stalled)
    # Substitutes the freshest available shared report instead
    # of surfacing a dead pipeline's last output as if it were current.
    # requested_type/is_fallback/is_stale/age_hours are carried through
    # unmodified so the calling agent can tell what it actually got.
    rep = _safe(report_get_raw, {})
    if isinstance(rep, dict) and 'report' in rep:
        r = rep['report']
        out['latest_report'] = {
            'type': r.get('report_type'),
            'requested_type': r.get('requested_type'),
            'is_fallback': r.get('is_fallback'),
            'is_stale': r.get('is_stale'),
            'age_hours': r.get('age_hours'),
            'created_at': r.get('created_at'),
            'summary': r.get('summary'),
            'id': r.get('id'),
        }
    else:
        out['latest_report'] = rep

    # Caller's own context (per-user key required — degrades gracefully).
    out['positions'] = _safe(trade_get_positions, {})
    out['plays'] = _safe(play_get_digest, {})
    out['watchlist'] = _safe(watchlist_manage, {'action': 'list'})

    out['next_steps'] = {
        'read_resources': [
            'carrac://guide/getting-started',
            'carrac://glossary/indicators',
            'carrac://glossary/quant-engine',
            # The three backtest models are not comparable, and
            # this is the only place that says so — an agent that reaches a
            # backtest tool without it will happily compare a Hold-N hit rate
            # against a Full-Sim win rate.
            'carrac://glossary/backtest-models',
            'carrac://grounding/rules',
            'carrac://facts/current',
        ],
        'try_prompts': ['analyze-asset', 'validate-trade', 'design-trade'],
        'tip': (
            'Ground every claim in tool results or carrac://facts/current — never '
            'invent macro numbers. These data are information to evaluate, not advice.'
        ),
    }
    out['next_steps']['backtesting'] = _backtesting_hint()
    return out
