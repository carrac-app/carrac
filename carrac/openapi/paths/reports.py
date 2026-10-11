"""Report endpoints."""

_AUTH = [{'api_key': []}, {'session_cookie': []}]
_TAG = 'Reports'
_ERR = {
    '401': {'description': 'Authentication required', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '429': {'description': 'Rate limit exceeded'},
}


def get_paths():
    return {
        '/api/report/morning/latest': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get latest morning report',
                'description': 'AI-generated morning analysis with macro context, technical analysis, and trade ideas.',
                'operationId': 'getLatestMorningReport',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Morning report content',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'content': {'type': 'string', 'description': 'Report HTML content'},
                                'audio_script': {'type': 'string', 'description': 'Audio narration script'},
                                'report_id': {'type': 'integer'},
                                'metadata': {'type': 'object'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/report/morning/recent': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get recent reports of a given type',
                'description': 'Last N reports of the given type for historical context. '
                               'Defaults to "morning" for back-compat; the weekly digest '
                               'passes type=general (the report type that keeps generating '
                               'daily regardless of the morning-report kill switch).',
                'operationId': 'getRecentMorningReports',
                'parameters': [
                    {'name': 'limit', 'in': 'query', 'required': False,
                     'schema': {'type': 'integer', 'default': 5}},
                    {'name': 'type', 'in': 'query', 'required': False,
                     'schema': {'type': 'string', 'default': 'morning',
                                'enum': ['morning', 'general', 'afternoon', 'weekly', 'week_ahead']}},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'List of recent reports'},
                    **_ERR,
                },
            },
        },
        '/api/report/weekly/latest': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get latest weekly digest',
                'operationId': 'getLatestWeeklyReport',
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Weekly digest content'},
                    **_ERR,
                },
            },
        },
        '/api/report/week_ahead/latest': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get latest week-ahead report',
                'operationId': 'getLatestWeekAheadReport',
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Week-ahead report content (`content` is null when none exists yet)'},
                    **_ERR,
                },
            },
        },
        '/api/report/afternoon/latest': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get latest afternoon update',
                'operationId': 'getLatestAfternoonReport',
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Afternoon update content'},
                    **_ERR,
                },
            },
        },
    }
