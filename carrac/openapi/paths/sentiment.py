"""Sentiment endpoints."""

_AUTH = [{'api_key': []}, {'session_cookie': []}]
_TAG = 'Sentiment'
_FORMAT = {'name': 'format', 'in': 'query', 'required': False,
           'schema': {'type': 'string', 'enum': ['compact']}}
_ERR = {
    '401': {'description': 'Authentication required', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '429': {'description': 'Rate limit exceeded'},
}


def get_paths():
    return {
        '/api/market-sentiment': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get market sentiment composite',
                'description': (
                    'Fear & Greed index, funding rates, long/short ratios, '
                    'MVRV Z-Score, alt season index, open interest, and regime score.'
                ),
                'operationId': 'getMarketSentiment',
                'parameters': [_FORMAT],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Full sentiment data',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'status': {'type': 'string'},
                                'timestamp': {'type': 'string', 'format': 'date-time'},
                                'sentiment': {
                                    'type': 'object',
                                    'properties': {
                                        'fear_greed': {'type': 'object'},
                                        'long_short': {'type': 'object'},
                                        'alt_season': {'type': 'object'},
                                        'mvrv_zscore': {'type': 'object'},
                                        'funding_rate': {'type': 'object'},
                                        'open_interest': {'type': 'object'},
                                        'regime': {'type': 'object'},
                                    },
                                },
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/sentiment-history': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get sentiment history',
                'description': 'Historical snapshots of sentiment metrics for charting.',
                'operationId': 'getSentimentHistory',
                'parameters': [
                    {'name': 'days', 'in': 'query', 'required': False,
                     'schema': {'type': 'integer', 'default': 30}},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Time series of sentiment snapshots',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'status': {'type': 'string'},
                                'count': {'type': 'integer'},
                                'snapshots': {'type': 'array', 'items': {'type': 'object'}},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
    }
