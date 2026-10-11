"""Plays (trade thesis tracking) endpoints."""

_AUTH = [{'api_key': []}, {'session_cookie': []}]
_TAG = 'Plays'
_FORMAT = {'name': 'format', 'in': 'query', 'required': False,
           'schema': {'type': 'string', 'enum': ['compact']}}
_ERR = {
    '401': {'description': 'Authentication required', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '403': {'description': "Your plan is below this operation's x-min-tier", 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '429': {'description': 'Rate limit exceeded'},
}
_PLAY_ID = {'name': 'play_id', 'in': 'path', 'required': True,
            'schema': {'type': 'integer'}}
_DRAWING_BODY = {
    'type': 'object',
    'required': ['drawing_id'],
    'properties': {'drawing_id': {'type': 'integer', 'minimum': 1}},
    'additionalProperties': False,
}


def get_paths():
    return {
        '/api/plays': {
            'get': {
                'tags': [_TAG],
                'summary': 'List plays',
                'description': 'Returns all plays for the authenticated user with live condition status.',
                'operationId': 'listPlays',
                'parameters': [
                    {'name': 'status', 'in': 'query', 'required': False,
                     'schema': {'type': 'string', 'enum': ['watching', 'acting', 'completed', 'invalidated', 'expired']}},
                    {'name': 'asset', 'in': 'query', 'required': False,
                     'schema': {'type': 'string'}, 'description': 'Filter by primary asset'},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'List of plays',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'plays': {'type': 'array', 'items': {'$ref': '#/components/schemas/Play'}},
                                'count': {'type': 'integer'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
            'post': {
                'tags': [_TAG],
                'summary': 'Create a new play',
                'operationId': 'createPlay',
                'x-min-tier': 'member',
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'required': ['title', 'thesis', 'direction', 'primary_asset'],
                        'properties': {
                            'title': {'type': 'string'},
                            'thesis': {'type': 'string'},
                            'type': {'type': 'string'},
                            'direction': {'type': 'string'},
                            'primary_asset': {'type': 'string'},
                            'conditions': {'type': 'object'},
                            'invalidation': {'type': 'object'},
                            'timeframe': {'type': 'string'},
                            'action_plan': {'type': 'string'},
                        },
                    }}},
                },
                'responses': {
                    '201': {
                        'description': 'Play created',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {'play': {'$ref': '#/components/schemas/Play'}},
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/plays/{play_id}': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get a single play',
                'operationId': 'getPlay',
                'parameters': [_PLAY_ID],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Play details', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Play'}}}},
                    '404': {'description': 'Play not found'},
                    **_ERR,
                },
            },
            'put': {
                'tags': [_TAG],
                'summary': 'Update a play',
                'operationId': 'updatePlay',
                'x-min-tier': 'member',
                'parameters': [_PLAY_ID],
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {'type': 'object'}}},
                },
                'responses': {
                    '200': {'description': 'Play updated', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Play'}}}},
                    **_ERR,
                },
            },
            'delete': {
                'tags': [_TAG],
                'summary': 'Delete a play',
                'operationId': 'deletePlay',
                'x-min-tier': 'member',
                'parameters': [_PLAY_ID],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Play deleted'},
                    **_ERR,
                },
            },
        },
        '/api/plays/{play_id}/link-drawing': {
            'post': {
                'tags': [_TAG],
                'summary': 'Link a chart drawing to a play',
                'description': 'Attaches one of your drawings to the play. Linking twice is harmless.',
                'operationId': 'linkPlayDrawing',
                'parameters': [_PLAY_ID],
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': _DRAWING_BODY}},
                },
                'responses': {
                    '200': {'description': 'The updated play, as `{play}`'},
                    '404': {'description': 'Play not found'},
                    **_ERR,
                },
            },
        },
        '/api/plays/{play_id}/unlink-drawing': {
            'post': {
                'tags': [_TAG],
                'summary': 'Unlink a chart drawing from a play',
                'description': 'Detaches the drawing from the play. The drawing itself is kept.',
                'operationId': 'unlinkPlayDrawing',
                'parameters': [_PLAY_ID],
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': _DRAWING_BODY}},
                },
                'responses': {
                    '200': {'description': 'The updated play, as `{play}`'},
                    '404': {'description': 'Play not found'},
                    **_ERR,
                },
            },
        },
        '/api/s/{slug}': {
            'get': {
                'tags': [_TAG],
                'summary': 'Read a shared snapshot',
                'description': (
                    'Public, no authentication. The frozen snapshot behind a share link '
                    '(`/s/{slug}`), exactly as it was when the owner minted it.'
                ),
                'operationId': 'getShare',
                'parameters': [{'name': 'slug', 'in': 'path', 'required': True,
                                'schema': {'type': 'string'}}],
                'security': [],
                'responses': {
                    '200': {'description': 'The snapshot, as `{slug, snapshot}`'},
                    '404': {'description': 'No such share'},
                    '410': {'description': 'The owner revoked the share'},
                },
            },
        },
        '/api/plays/{play_id}/status': {
            'put': {
                'tags': [_TAG],
                'summary': 'Update play status',
                'description': 'Transition a play to completed, invalidated, or expired with a reason.',
                'operationId': 'updatePlayStatus',
                'parameters': [_PLAY_ID],
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'required': ['status'],
                        'properties': {
                            'status': {'type': 'string', 'enum': ['completed', 'invalidated', 'expired']},
                            'reason': {'type': 'string'},
                        },
                    }}},
                },
                'responses': {
                    '200': {'description': 'Status updated'},
                    **_ERR,
                },
            },
        },
        '/api/plays/{play_id}/evaluate': {
            'post': {
                'tags': [_TAG],
                'summary': 'Force re-evaluate play conditions',
                'description': 'Triggers immediate condition re-evaluation against current market data.',
                'operationId': 'evaluatePlay',
                'parameters': [_PLAY_ID],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Evaluation result',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'play': {'$ref': '#/components/schemas/Play'},
                                'evaluation': {
                                    'type': 'object',
                                    'properties': {
                                        'met_count': {'type': 'integer'},
                                        'total_count': {'type': 'integer'},
                                        'all_met': {'type': 'boolean'},
                                        'invalidation_triggered': {'type': 'boolean'},
                                        'expired': {'type': 'boolean'},
                                    },
                                },
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/plays/{play_id}/backtest': {
            'post': {
                'tags': [_TAG],
                'summary': 'Hold-N backtest of play conditions (custom lookback / hold)',
                'description': (
                    'Paid-tier custom backtest. Accepts lookback_bars (clamped to 50..2000) '
                    'and hold_bars (clamped to 1..60) in the JSON body. Uses the '
                    '`backtester` limit (member and up).'
                ),
                'operationId': 'backtestPlay',
                'x-min-tier': 'member',
                'parameters': [_PLAY_ID],
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'properties': {
                            'lookback_bars': {'type': 'integer', 'minimum': 1, 'default': 365},
                            'hold_bars': {'type': 'integer', 'minimum': 1, 'default': 5},
                        },
                    }}},
                },
                'responses': {
                    '200': {'description': 'Backtest result'},
                    '400': {'description': 'No backtestable conditions'},
                    '404': {'description': 'Play not found'},
                    **_ERR,
                },
            },
        },
        '/api/plays/active': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get active plays (watching + acting)',
                'operationId': 'getActivePlays',
                'x-min-tier': 'member',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Active plays list',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'plays': {'type': 'array', 'items': {'$ref': '#/components/schemas/Play'}},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/plays/context': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get AI-optimized plays context',
                'description': 'Human-readable summary of active plays, optimized for morning report and AI chat.',
                'operationId': 'getPlaysContext',
                'parameters': [_FORMAT],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Plays context summary',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'plays': {'type': 'array', 'items': {'type': 'object'}},
                                'summary': {'type': 'string'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
    }
