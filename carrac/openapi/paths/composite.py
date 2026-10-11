"""Composite endpoints — asset detail, live positions, batch query."""

from carrac.composite.min_tiers import (
    ASSET_DETAIL_MIN_TIER,
    BATCH_MIN_TIER,
    LIVE_POSITIONS_MIN_TIER,
)

_AUTH = [{'api_key': []}, {'session_cookie': []}]
_TAG = 'Composite'
_FORMAT = {'name': 'format', 'in': 'query', 'required': False,
           'schema': {'type': 'string', 'enum': ['compact']},
           'description': 'Set to "compact" for token-efficient summary'}
_ERR = {
    '401': {'description': 'Authentication required', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '403': {'description': "Your plan is below this operation's x-min-tier", 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '429': {'description': 'Rate limit exceeded'},
}


def get_paths():
    return {
        '/api/asset/{symbol}/detail': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get everything about one asset',
                'description': (
                    'Assembles technicals, levels, scanner scores, sentiment, '
                    'plays, alerts, drawings, and open positions for a single asset '
                    'in one call. Replaces 6-8 separate API calls.\n\n'
                    'Default uses precomputed caches (<100ms). '
                    'All user-specific data is tied to the authenticated user.'
                ),
                'operationId': 'getAssetDetail',
                'x-min-tier': ASSET_DETAIL_MIN_TIER,
                'parameters': [
                    {'name': 'symbol', 'in': 'path', 'required': True,
                     'schema': {'type': 'string'}, 'example': 'BTC'},
                    _FORMAT,
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Complete asset detail',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'symbol': {'type': 'string'},
                                'price': {'type': 'number'},
                                'change_24h': {'type': 'number'},
                                'technicals': {'type': 'object', 'description': 'Consensio, RSI, EMA ribbon, ADX, SuperTrend, etc.'},
                                'levels': {'type': 'object', 'description': 'CPR (monthly/weekly/daily), PDH, PDL, PWH, PWL'},
                                'scanner': {'type': 'object', 'description': 'Long/short scores and verdicts'},
                                'sentiment': {'type': 'object', 'description': 'Market regime and score'},
                                'plays': {'type': 'array', 'items': {'type': 'object'}, 'description': 'Active plays for this asset'},
                                'alerts': {'type': 'array', 'items': {'$ref': '#/components/schemas/Alert'}},
                                'drawings': {'type': 'array', 'items': {'type': 'object'}},
                                'positions': {'type': 'array', 'items': {'type': 'object'}, 'description': 'Open trades with live PnL'},
                                'availability': {'type': 'object', 'description': 'Per-source status (ok/error)'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/positions/live': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get open positions with live market data',
                'description': (
                    'Enriches open journal trades with current price, unrealized PnL, '
                    'R-multiple, risk status, scanner verdict, and linked play status. '
                    'Includes portfolio heat summary.'
                ),
                'operationId': 'getLivePositions',
                'x-min-tier': LIVE_POSITIONS_MIN_TIER,
                'parameters': [
                    {'name': 'asset', 'in': 'query', 'required': False,
                     'schema': {'type': 'string'}, 'description': 'Filter to one asset'},
                    _FORMAT,
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Enriched open positions',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'positions': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'id': {'type': 'integer'},
                                            'asset': {'type': 'string'},
                                            'direction': {'type': 'string', 'enum': ['long', 'short']},
                                            'entry_price': {'type': 'number'},
                                            'current_price': {'type': 'number'},
                                            'stop_price': {'type': 'number'},
                                            'target_price': {'type': 'number'},
                                            'unrealized_pnl_pct': {'type': 'number'},
                                            'current_rr': {'type': 'number'},
                                            'risk_status': {'type': 'string', 'enum': ['profitable', 'neutral', 'at_risk', 'stopped_out']},
                                            'scanner_verdict': {'type': 'string'},
                                            'linked_play': {'type': 'object'},
                                            'time_in_trade': {'type': 'string'},
                                        },
                                    },
                                },
                                'portfolio_heat': {
                                    'type': 'object',
                                    'properties': {
                                        'total_exposure': {'type': 'integer'},
                                        'long_count': {'type': 'integer'},
                                        'short_count': {'type': 'integer'},
                                    },
                                },
                                'availability': {'type': 'object'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/batch': {
            'get': {
                'tags': [_TAG],
                'summary': 'Multi-asset, multi-data-type query',
                'description': (
                    'Fetch multiple data types for multiple assets in a single call. '
                    'Supports 7 data blocks: market, scanner, sentiment, alerts, plays, '
                    'positions, drawings. Maximum 10 assets per request.\n\n'
                    'Sentiment data is global (not per-asset) and returned under the `global` key.'
                ),
                'operationId': 'batchQuery',
                'x-min-tier': BATCH_MIN_TIER,
                'parameters': [
                    {'name': 'assets', 'in': 'query', 'required': True,
                     'schema': {'type': 'string'},
                     'description': 'Comma-separated symbols (max 10)', 'example': 'BTC,ETH,SOL'},
                    {'name': 'include', 'in': 'query', 'required': False,
                     'schema': {'type': 'string', 'default': 'market,scanner'},
                     'description': 'Comma-separated data blocks: market, scanner, sentiment, alerts, plays, positions, drawings'},
                    _FORMAT,
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Per-asset data blocks + global data',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'assets': {
                                    'type': 'object',
                                    'description': 'Keyed by symbol, each containing requested data blocks',
                                    'additionalProperties': {'type': 'object'},
                                },
                                'global': {
                                    'type': 'object',
                                    'description': 'Non-per-asset data (e.g. sentiment)',
                                },
                                'availability': {'type': 'object'},
                            },
                        }}},
                    },
                    '400': {
                        'description': 'Invalid parameters',
                        'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}},
                    },
                    **_ERR,
                },
            },
        },
    }
