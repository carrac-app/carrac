"""Chart data endpoints."""

_AUTH = [{'api_key': []}, {'session_cookie': []}]
_TAG = 'Chart'

_SYMBOL = {'name': 'symbol', 'in': 'path', 'required': True,
           'schema': {'type': 'string'}, 'example': 'BTC'}
_FORMAT = {'name': 'format', 'in': 'query', 'required': False,
           'schema': {'type': 'string', 'enum': ['compact']},
           'description': 'Set to "compact" for token-efficient summary'}
_TF = {'name': 'tf', 'in': 'query', 'required': False,
       'schema': {'type': 'string', 'enum': ['1h', '4h', '1d', '3d', '1w', '1M']},
       'description': 'Timeframe (default: 1d)'}
_BARS = {'name': 'bars', 'in': 'query', 'required': False,
         'schema': {'type': 'integer', 'default': 2500},
         'description': 'Number of OHLCV bars'}

_ERR = {
    '401': {'description': 'Authentication required', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '403': {'description': "Your plan is below this operation's x-min-tier", 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '429': {'description': 'Rate limit exceeded'},
}


def get_paths():
    return {
        '/api/chart/{symbol}': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get chart data with indicators',
                'description': (
                    'Returns OHLCV candles, volume, indicators (RSI, MACD, Consensio, '
                    'EMA ribbon, structure), and CPR levels (monthly, weekly, daily). '
                    'With `?format=compact`, returns a ~800 byte summary with price, '
                    'change%, key levels, and indicator states.'
                ),
                'operationId': 'getChart',
                'parameters': [_SYMBOL, _TF, _BARS, _FORMAT],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Chart data with indicators and levels',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'ohlcv': {'type': 'array', 'items': {'$ref': '#/components/schemas/OHLCV'}},
                                'volume': {'type': 'array', 'items': {'$ref': '#/components/schemas/Volume'}},
                                'symbol': {'type': 'string'},
                                'timeframe': {'type': 'string'},
                                'cpr': {'$ref': '#/components/schemas/CPR'},
                                'weekly_cpr': {'$ref': '#/components/schemas/CPR'},
                                'daily_cpr': {'$ref': '#/components/schemas/CPR'},
                                'indicators': {'type': 'object', 'description': 'Computed indicator values'},
                                'indicators_ext': {'type': 'object', 'description': 'Extended indicator snapshot'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/chart-internal/{symbol}': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get internal chart data (for headless rendering)',
                'description': (
                    'Same data as /api/chart but includes additional fields for '
                    'server-side chart rendering. Authenticated via `key` query param.'
                ),
                'operationId': 'getChartInternal',
                'parameters': [
                    _SYMBOL, _TF, _BARS, _FORMAT,
                    {'name': 'key', 'in': 'query', 'required': True,
                     'schema': {'type': 'string'}, 'description': 'API key'},
                ],
                'responses': {
                    '200': {'description': 'Internal chart data'},
                    **_ERR,
                },
            },
        },
        '/api/consensio/{symbol}': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get Consensio indicator data',
                'operationId': 'getConsensio',
                'parameters': [_SYMBOL],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Consensio multi-timeframe alignment data'},
                    **_ERR,
                },
            },
        },
        '/api/volume-profile/{symbol}': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get volume profile data',
                'operationId': 'getVolumeProfile',
                'parameters': [_SYMBOL],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Volume profile with POC, value area'},
                    **_ERR,
                },
            },
        },
        '/api/relative-strength': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get relative strength rankings',
                'operationId': 'getRelativeStrength',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Ranked assets by relative strength',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'assets': {'type': 'array', 'items': {'type': 'object'}},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/weekly-signal': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get BTC weekly cycle position',
                'description': (
                    'Where BTC sits relative to its 200-week moving average, '
                    'with MVRV Z-score and Puell multiple as supporting '
                    'readings. `signal` is a descriptive zone: DISCOUNT '
                    '(at, below or near the 200-week MA), NEUTRAL or EXTENDED '
                    '(more than double it); NO_DATA when the average cannot '
                    'be computed. `reason` states the distance from the '
                    'average. BTC only.'
                ),
                'operationId': 'getWeeklySignal',
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Weekly cycle position and its readings'},
                    **_ERR,
                },
            },
        },
        '/api/health/hyperliquid-ws': {
            'get': {
                'tags': [_TAG],
                'summary': 'Hyperliquid live-candle stream health',
                'description': 'Connection state, stale coins and reconnection history of the Hyperliquid candle stream.',
                'operationId': 'getHyperliquidStreamHealth',
                'security': _AUTH,
                'responses': {'200': {'description': 'Stream health'}, **_ERR},
            },
        },
        '/api/health/ccxt-ws': {
            'get': {
                'tags': [_TAG],
                'summary': 'Exchange live-candle stream health',
                'description': 'Connection state of the Gate.io and Bybit candle streams.',
                'operationId': 'getExchangeStreamHealth',
                'security': _AUTH,
                'responses': {'200': {'description': 'Stream health'}, **_ERR},
            },
        },
    }
