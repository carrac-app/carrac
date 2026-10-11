"""Journal trade endpoints.

Request bodies are derived from the Pydantic models the FastAPI router
validates with, so the published schema cannot drift from what the server
accepts.
"""

from carrac.fastapi_app.models.journal import (
    ChecklistRequest,
    TradeCloseRequest,
    TradeCreateRequest,
    TradeUpdateRequest,
)

_AUTH = [{'api_key': []}, {'session_cookie': []}]
_TAG = 'Journal'
_ERR = {
    '401': {'description': 'Authentication required', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '403': {'description': "Your plan is below this operation's x-min-tier", 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '429': {'description': 'Rate limit exceeded'},
}
_AUTH_NOTE = ' Authenticate with a per-user API key in `X-API-Key`, or a signed-in session.'
_TRADE_ID = {'name': 'trade_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}
_SCREENSHOT_ID = {'name': 'screenshot_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}
_CHECKLIST_ID = {'name': 'checklist_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}


def _openapi_schema(model):
    """The model's JSON Schema, expressed in OpenAPI 3.0 terms.

    Pydantic writes ``Optional[X]`` as ``anyOf: [X, {type: null}]``; 3.0 has
    ``nullable`` instead, and a numeric ``exclusiveMinimum`` becomes
    ``minimum`` plus a boolean flag. Titles are dropped as noise.
    """
    def convert(node):
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        node = {key: convert(value) for key, value in node.items() if key != 'title'}
        branches = node.get('anyOf')
        if branches:
            kept = [b for b in branches if b != {'type': 'null'}]
            if len(kept) != len(branches):
                node.pop('anyOf')
                if len(kept) == 1:
                    node.update(kept[0])
                else:
                    node['anyOf'] = kept
                node['nullable'] = True
        for bound, limit in (('exclusiveMinimum', 'minimum'), ('exclusiveMaximum', 'maximum')):
            if isinstance(node.get(bound), (int, float)) and not isinstance(node[bound], bool):
                node[limit] = node[bound]
                node[bound] = True
        return node

    return convert(model.model_json_schema())


def get_paths():
    return {
        '/api/journal/trades': {
            'get': {
                'tags': [_TAG],
                'summary': 'List journal trades',
                'description': 'Returns open and closed trades with stats (win rate, avg RR, PnL).',
                'operationId': 'listTrades',
                'parameters': [
                    {'name': 'asset', 'in': 'query', 'required': False, 'schema': {'type': 'string'}},
                    {'name': 'status', 'in': 'query', 'required': False,
                     'schema': {'type': 'string', 'enum': ['open', 'closed']}},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Trades grouped by open/closed with stats',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'open': {'type': 'array', 'items': {'$ref': '#/components/schemas/Trade'}},
                                'history': {'type': 'array', 'items': {'$ref': '#/components/schemas/Trade'}},
                                'stats': {
                                    'type': 'object',
                                    'properties': {
                                        'total_trades': {'type': 'integer'},
                                        'win_rate': {'type': 'number'},
                                        'avg_rr': {'type': 'number'},
                                        'pnl_total': {'type': 'number'},
                                    },
                                },
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
            'post': {
                'tags': [_TAG],
                'summary': 'Log a new trade',
                'description': 'Unknown fields are rejected with 400.' + _AUTH_NOTE,
                'operationId': 'createTrade',
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': _openapi_schema(TradeCreateRequest)}},
                },
                'responses': {
                    '201': {
                        'description': 'Trade created',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {'id': {'type': 'integer'}},
                        }}},
                    },
                    '400': {'description': 'Invalid body'},
                    **_ERR,
                },
            },
        },
        '/api/journal/trades/{trade_id}': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get trade detail',
                'operationId': 'getTrade',
                'parameters': [_TRADE_ID],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Full trade detail with screenshots and checklist'},
                    '404': {'description': 'Trade not found'},
                    **_ERR,
                },
            },
            'patch': {
                'tags': [_TAG],
                'summary': 'Update a trade',
                'description': (
                    'Partial update: only the fields sent change. Unknown fields, '
                    'including the AI-only `ai_review` and `trade_grade`, are rejected with 400.'
                    + _AUTH_NOTE
                ),
                'operationId': 'updateTrade',
                'parameters': [_TRADE_ID],
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': _openapi_schema(TradeUpdateRequest)}},
                },
                'responses': {
                    '200': {'description': 'Trade updated; the body is `{ok, entry}`'},
                    '400': {'description': 'Invalid body'},
                    '404': {'description': 'Trade not found'},
                    **_ERR,
                },
            },
            'delete': {
                'tags': [_TAG],
                'summary': 'Delete a trade',
                'operationId': 'deleteTrade',
                'parameters': [_TRADE_ID],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Trade deleted'},
                    **_ERR,
                },
            },
        },
        '/api/journal/trades/{trade_id}/close': {
            'post': {
                'tags': [_TAG],
                'summary': 'Close a trade',
                'description': (
                    'Records the exit and computes the realised R multiple.' + _AUTH_NOTE
                ),
                'operationId': 'closeTrade',
                'parameters': [_TRADE_ID],
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': _openapi_schema(TradeCloseRequest)}},
                },
                'responses': {
                    '200': {'description': 'Trade closed; the body is `{ok, entry}`'},
                    '400': {'description': 'Invalid body'},
                    '404': {'description': 'Trade not found'},
                    **_ERR,
                },
            },
        },
        '/api/journal/trades/{trade_id}/reopen': {
            'post': {
                'tags': [_TAG],
                'summary': 'Reopen a closed trade',
                'description': 'Undoes a close: the exit fields are cleared and the trade is open again.' + _AUTH_NOTE,
                'operationId': 'reopenTrade',
                'parameters': [_TRADE_ID],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Trade reopened; the body is `{ok}`'},
                    '404': {'description': 'Trade not found'},
                    **_ERR,
                },
            },
        },
        '/api/journal/trades/{trade_id}/screenshot': {
            'post': {
                'tags': [_TAG],
                'summary': 'Attach a chart screenshot to a trade',
                'description': (
                    'Renders the trade chart now and stores the image with the trade. '
                    'A trade holds at most 10 screenshots.' + _AUTH_NOTE
                ),
                'operationId': 'captureTradeScreenshot',
                'parameters': [_TRADE_ID],
                'security': _AUTH,
                'requestBody': {
                    'required': False,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'properties': {'type': {
                            'type': 'string',
                            'enum': ['entry', 'exit', 'management', 'context'],
                            'default': 'context',
                        }},
                    }}},
                },
                'responses': {
                    '201': {'description': 'Screenshot stored; the body is `{screenshot_id, file_path, type}`'},
                    '400': {'description': 'Invalid type, or the trade already has 10 screenshots'},
                    '404': {'description': 'Trade not found'},
                    '500': {'description': 'The chart could not be rendered'},
                    **_ERR,
                },
            },
        },
        '/api/journal/trades/{trade_id}/screenshots': {
            'get': {
                'tags': [_TAG],
                'summary': 'List a trade\'s screenshots',
                'operationId': 'listTradeScreenshots',
                'parameters': [_TRADE_ID],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Screenshots, oldest first'},
                    '404': {'description': 'Trade not found'},
                    **_ERR,
                },
            },
        },
        '/api/journal/screenshots/{screenshot_id}': {
            'delete': {
                'tags': [_TAG],
                'summary': 'Delete a trade screenshot',
                'description': 'Removes the stored image and its record.' + _AUTH_NOTE,
                'operationId': 'deleteTradeScreenshot',
                'parameters': [_SCREENSHOT_ID],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Screenshot deleted'},
                    '404': {'description': 'Screenshot not found'},
                    **_ERR,
                },
            },
        },
        '/api/journal/checklists': {
            'get': {
                'tags': [_TAG],
                'summary': 'List setup checklists',
                'operationId': 'listChecklists',
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'One checklist template per setup type'},
                    **_ERR,
                },
            },
            'post': {
                'tags': [_TAG],
                'summary': 'Create or update a setup checklist',
                'description': (
                    'Upsert by `setup_type`: a second call for the same setup type replaces '
                    'its checklist.' + _AUTH_NOTE
                ),
                'operationId': 'upsertChecklist',
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': _openapi_schema(ChecklistRequest)}},
                },
                'responses': {
                    '200': {'description': 'Checklist saved; the body is `{id, ok}`'},
                    '400': {'description': 'Invalid body'},
                    **_ERR,
                },
            },
        },
        '/api/journal/checklists/{checklist_id}': {
            'delete': {
                'tags': [_TAG],
                'summary': 'Delete a setup checklist',
                'operationId': 'deleteChecklist',
                'parameters': [_CHECKLIST_ID],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Checklist deleted'},
                    '404': {'description': 'Checklist not found'},
                    **_ERR,
                },
            },
        },
        '/api/journal/analytics': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get trade analytics',
                'description': 'Win rate, Sharpe ratio, drawdown, equity curve, and playbook stats.',
                'operationId': 'getTradeAnalytics',
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Analytics summary'},
                    **_ERR,
                },
            },
        },
    }
