"""Drawing/annotation endpoints."""

_AUTH = [{'api_key': []}, {'session_cookie': []}]
_TAG = 'Drawings'
_ERR = {
    '401': {'description': 'Authentication required', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '403': {'description': "Your plan is below this operation's x-min-tier", 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '429': {'description': 'Rate limit exceeded'},
}
_DRAWING_TYPES = [
    'horizontal_line', 'zone', 'annotation', 'trendline', 'fibonacci', 'position',
    'long_position', 'short_position', 'spot', 'scenario', 'liquidation_zone',
]
_ERROR_BODY = {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}


def _drawing_fields():
    """Properties shared by the create and update bodies."""
    return {
        'asset': {'type': 'string', 'minLength': 1, 'maxLength': 20},
        'timeframe': {'type': 'string', 'maxLength': 10},
        'drawing_type': {'type': 'string', 'enum': _DRAWING_TYPES},
        'price_level': {'type': 'number', 'minimum': 0},
        'price_level_upper': {'type': 'number', 'minimum': 0},
        'label': {'type': 'string', 'maxLength': 500},
        'color': {'type': 'string', 'description': 'Hex color: #RGB, #RRGGBB or #RRGGBBAA.'},
        'line_style': {'type': 'string', 'enum': ['solid', 'dashed', 'dotted']},
        'line_width': {'type': 'integer', 'minimum': 1, 'maximum': 10},
        'metadata': {'type': 'object'},
    }


def _create_body():
    return {
        'type': 'object',
        'additionalProperties': False,
        'required': ['asset', 'timeframe', 'drawing_type'],
        'properties': {
            **_drawing_fields(),
            'scenario_id': {'type': 'integer', 'minimum': 1,
                            'description': 'Parent scenario drawing, for child drawings.'},
        },
    }


_DRAWING_ID = {'name': 'drawing_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}


def get_paths():
    return {
        '/api/drawings': {
            'get': {
                'tags': [_TAG],
                'summary': 'List drawings',
                'description': 'Returns drawings for the authenticated user, optionally filtered by asset.',
                'operationId': 'listDrawings',
                'parameters': [
                    {'name': 'asset', 'in': 'query', 'required': False, 'schema': {'type': 'string'}},
                    {'name': 'timeframe', 'in': 'query', 'required': False, 'schema': {'type': 'string'}},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'List of drawings',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'drawings': {'type': 'array', 'items': {'$ref': '#/components/schemas/Drawing'}},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
            'post': {
                'tags': [_TAG],
                'summary': 'Create a drawing',
                'operationId': 'createDrawing',
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': _create_body()}},
                },
                'responses': {
                    '201': {
                        'description': 'Drawing created',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'success': {'type': 'boolean'},
                                'drawing_id': {'type': 'integer'},
                            },
                        }}},
                    },
                    '400': {'description': 'Invalid body', 'content': _ERROR_BODY},
                    **_ERR,
                },
            },
        },
        '/api/drawings/{drawing_id}': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get a drawing',
                'operationId': 'getDrawing',
                'parameters': [_DRAWING_ID],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Drawing detail'},
                    '404': {'description': 'Drawing not found'},
                    **_ERR,
                },
            },
            'put': {
                'tags': [_TAG],
                'summary': 'Update a drawing',
                'description': (
                    'Send only the fields to change; unknown fields are rejected with 400. '
                    'Pass the version you last read as `if_version` (or an `If-Match` header) '
                    'to guard against overwriting a newer edit; a stale version answers 409. '
                    'Timestamps in drawing responses are ISO 8601.'
                ),
                'operationId': 'updateDrawing',
                'parameters': [_DRAWING_ID],
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'additionalProperties': False,
                        'properties': {
                            **_drawing_fields(),
                            'if_version': {'type': 'integer',
                                           'description': 'Version the edit is based on (optimistic concurrency).'},
                        },
                    }}},
                },
                'responses': {
                    '200': {'description': 'Drawing updated'},
                    '400': {'description': 'Invalid body, or no valid fields to update', 'content': _ERROR_BODY},
                    '403': {'description': 'Not your drawing', 'content': _ERROR_BODY},
                    '404': {'description': 'Drawing not found', 'content': _ERROR_BODY},
                    '409': {'description': 'Version conflict: the drawing changed since `if_version`', 'content': _ERROR_BODY},
                    **_ERR,
                },
            },
            'delete': {
                'tags': [_TAG],
                'summary': 'Delete a drawing',
                'operationId': 'deleteDrawing',
                'parameters': [_DRAWING_ID],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Drawing deleted'},
                    **_ERR,
                },
            },
        },
        '/api/drawings/upsert': {
            'post': {
                'tags': [_TAG],
                'summary': 'Create or update a drawing by local id',
                'description': 'Same body as `createDrawing`, with `metadata.local_id` required: the drawing carrying that local id is updated, or created if there is none.',
                'operationId': 'upsertDrawing',
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': _create_body()}},
                },
                'responses': {
                    '200': {'description': 'Existing drawing updated (`action: updated`)'},
                    '201': {'description': 'Drawing created (`action: created`)'},
                    '400': {'description': 'Invalid body, or `metadata.local_id` missing', 'content': _ERROR_BODY},
                    **_ERR,
                },
            },
        },
        '/api/drawings/bulk': {
            'post': {
                'tags': [_TAG],
                'summary': 'Create up to 50 drawings at once',
                'operationId': 'bulkCreateDrawings',
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'additionalProperties': False,
                        'required': ['drawings'],
                        'properties': {
                            'drawings': {'type': 'array', 'maxItems': 50, 'items': {
                                'type': 'object', 'additionalProperties': False,
                                'required': ['asset', 'timeframe', 'drawing_type'],
                                'properties': _drawing_fields(),
                            }},
                            'replace': {'type': 'boolean', 'default': False,
                                        'description': 'Delete the existing drawings first.'},
                        },
                    }}},
                },
                'responses': {
                    '201': {'description': 'All drawings created'},
                    '207': {'description': 'Some drawings failed; `errors` lists them'},
                    '400': {'description': 'Invalid body', 'content': _ERROR_BODY},
                    **_ERR,
                },
            },
        },
        '/api/drawings/by-local-id': {
            'delete': {
                'tags': [_TAG],
                'summary': 'Delete a drawing by local id',
                'operationId': 'deleteDrawingByLocalId',
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'additionalProperties': False,
                        'required': ['asset', 'local_id'],
                        'properties': {'asset': {'type': 'string'}, 'local_id': {'type': 'string'}},
                    }}},
                },
                'responses': {
                    '200': {'description': '`success` is false when no such drawing exists'},
                    **_ERR,
                },
            },
        },
        '/api/drawings/all': {
            'get': {
                'tags': [_TAG],
                'summary': 'List all drawings grouped by asset and timeframe',
                'operationId': 'getAllDrawingsGrouped',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': '`drawings` maps asset to timeframe to a list of drawings',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'success': {'type': 'boolean'},
                                'total_drawings': {'type': 'integer'},
                                'drawings': {'type': 'object', 'additionalProperties': {
                                    'type': 'object', 'additionalProperties': {
                                        'type': 'array', 'items': {'$ref': '#/components/schemas/Drawing'}}}},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/drawings/scenario/{scenario_id}/children': {
            'get': {
                'tags': [_TAG],
                'summary': 'List the drawings attached to a scenario',
                'operationId': 'getScenarioChildren',
                'parameters': [{'name': 'scenario_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Child drawings',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'success': {'type': 'boolean'},
                                'scenario_id': {'type': 'integer'},
                                'count': {'type': 'integer'},
                                'drawings': {'type': 'array', 'items': {'$ref': '#/components/schemas/Drawing'}},
                            },
                        }}},
                    },
                    '403': {'description': 'Not your scenario', 'content': _ERROR_BODY},
                    '404': {'description': 'Scenario not found', 'content': _ERROR_BODY},
                    **{k: v for k, v in _ERR.items() if k != '403'},
                },
            },
        },
        '/api/drawings/scenarios/assets': {
            'get': {
                'tags': [_TAG],
                'summary': 'List assets that have an active scenario',
                'operationId': 'getScenarioAssets',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Asset symbols',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'success': {'type': 'boolean'},
                                'assets': {'type': 'array', 'items': {'type': 'string'}},
                                'count': {'type': 'integer'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/drawings/context': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get human-readable drawing context',
                'description': (
                    'Returns chart annotations as human-readable descriptions per asset. '
                    'Includes trendlines with slope, channels with width, fibonacci levels, etc. '
                    'Designed for AI consumption (morning report, copilot).'
                ),
                'operationId': 'getDrawingsContext',
                'parameters': [
                    {'name': 'assets', 'in': 'query', 'required': False,
                     'schema': {'type': 'string'}, 'description': 'Comma-separated asset list, or "all"'},
                    {'name': 'enrich', 'in': 'query', 'required': False,
                     'schema': {'type': 'integer', 'enum': [0, 1]},
                     'description': 'Include price distance calculations'},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Per-asset drawing descriptions'},
                    **_ERR,
                },
            },
        },
        '/api/drawings/positions': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get position drawings',
                'description': 'Active position markers on charts with entry, stop, and target levels.',
                'operationId': 'getPositionDrawings',
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Position drawings'},
                    **_ERR,
                },
            },
        },
    }
