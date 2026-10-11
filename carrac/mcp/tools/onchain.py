"""MCP tools: On-chain data — Hyperliquid smart-money positioning."""

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError

_NON_ADVICE = (
    "This is observed positioning data, not financial advice. "
    "It reflects the net long/short bias of profitable Hyperliquid traders — "
    "not a signal to buy, sell, or take any position."
)

_BIAS_THRESHOLDS = (
    (65.0, "net long"),
    (55.0, "mildly net long"),
    (45.0, "balanced"),
    (35.0, "mildly net short"),
)


def _bias_label(value: float) -> str:
    """Map a SMART_MONEY_BIAS value (0–100) to a human label."""
    for threshold, label in _BIAS_THRESHOLDS:
        if value >= threshold:
            return label
    return "net short"


@mcp_tool(
    'onchain_get_smart_money',
    'Return the current smart-money positioning bias for a crypto asset on '
    'Hyperliquid — the net long/short bias of the top profitable traders '
    '(PnL-weighted by position size). '
    'Returns: asset, bias (0–100; 50=balanced, >50=net long, <50=net short), '
    'label (net long / mildly net long / balanced / mildly net short / net short), '
    'data_date (ISO date of the latest observation), '
    'summary (one-sentence positioning description), '
    'disclaimer (non-advice statement). '
    'Bias > 65 = clearly net long; < 35 = clearly net short; 45–55 = balanced. '
    'Call this to understand whether profitable Hyperliquid traders are positioned '
    'long or short on a specific asset. '
    'Data freshness: updated daily by the Hyperliquid collector. '
    'Raises ToolError when no data is available for the requested asset.',
    {
        'type': 'object',
        'properties': {
            'asset': {
                'type': 'string',
                'description': (
                    'Asset symbol as traded on Hyperliquid (e.g. BTC, ETH, SOL). '
                    'Required.'
                ),
            },
        },
        'required': ['asset'],
    },
    min_tier='premium',
)
def onchain_get_smart_money(arguments):
    asset = (arguments.get('asset') or '').strip().upper()
    if not asset:
        raise ToolError('asset is required')

    try:
        from carrac.db_pool import get_quant_conn as get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT value, ts FROM quant.onchain "
                "WHERE metric = 'SMART_MONEY_BIAS' AND asset = %s "
                "ORDER BY ts DESC LIMIT 1",
                (asset,),
            )
            row = cur.fetchone()
    except Exception as exc:
        raise ToolError(f'Smart-money positioning query failed: {exc}')

    if row is None:
        raise ToolError(
            f'No smart-money positioning data for {asset}. '
            f'Only assets actively traded on Hyperliquid with sufficient '
            f'top-trader data are available.'
        )

    bias = float(row['value'])
    label = _bias_label(bias)
    data_date = str(row['ts']) if row.get('ts') else 'unknown'
    summary = (
        f"Profitable Hyperliquid traders are currently {label} on {asset} "
        f"(bias score {bias:.0f}/100, where 50=balanced)."
    )

    return {
        'asset': asset,
        'bias': round(bias, 1),
        'label': label,
        'data_date': data_date,
        'summary': summary,
        'disclaimer': _NON_ADVICE,
    }
