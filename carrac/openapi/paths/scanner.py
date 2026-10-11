"""Scanner endpoints."""

_AUTH = [{'api_key': []}, {'session_cookie': []}]
_TAG = 'Scanner'
_FORMAT = {'name': 'format', 'in': 'query', 'required': False,
           'schema': {'type': 'string', 'enum': ['compact']}}
_ERR = {
    '401': {'description': 'Authentication required', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '403': {'description': "Your plan is below this operation's x-min-tier", 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '429': {'description': 'Rate limit exceeded'},
}


def get_paths():
    return {
        '/api/scanner': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get scanner results for all assets',
                'description': (
                    'Returns scored rankings for all tracked assets. Each asset has '
                    'long/short scores (0-12), verdicts (STRONG/GOOD/NEUTRAL/AVOID), '
                    'and per-criterion breakdown. Compact mode returns a summary.'
                ),
                'operationId': 'getScanner',
                'x-min-tier': 'member',
                'parameters': [
                    {'name': 'verdict', 'in': 'query', 'required': False,
                     'schema': {'type': 'string'},
                     'description': 'Comma-separated verdict filter (STRONG,GOOD,WATCH)'},
                    {'name': 'direction', 'in': 'query', 'required': False,
                     'schema': {'type': 'string', 'enum': ['long', 'short', 'both'],
                                'default': 'both'}},
                    _FORMAT,
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Scanner results with scores and verdicts',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'results': {'type': 'array', 'items': {'$ref': '#/components/schemas/ScannerResult'}},
                                'macro': {'type': 'object', 'description': 'Macro overlay scores'},
                                'computed_at': {'type': 'string', 'format': 'date-time'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/scanner/{symbol}': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get scanner detail for one asset',
                'operationId': 'getScannerDetail',
                'parameters': [
                    {'name': 'symbol', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Detailed scanner breakdown for one asset'},
                    **_ERR,
                },
            },
        },
        '/api/scanner/stats': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get scanner historical accuracy stats',
                'operationId': 'getScannerStats',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Win rates by criterion and verdict',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'total_trades': {'type': 'integer'},
                                'overall_win_rate': {'type': 'number'},
                                'by_criterion': {'type': 'object'},
                                'by_verdict': {'type': 'object'},
                                'sample_sufficient': {'type': 'boolean'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/stream': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get live confluence zones and signals',
                'operationId': 'getLiveStream',
                'parameters': [_FORMAT],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Confluence zones with sources and strength scores'},
                    **_ERR,
                },
            },
        },
        '/api/transitions': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get recent indicator state transitions',
                'operationId': 'getTransitions',
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Recent indicator flips across all assets'},
                    **_ERR,
                },
            },
        },
    }
