"""AI assistant endpoints."""

_AUTH = [{'api_key': []}, {'session_cookie': []}]
_TAG = 'AI'
_ERR = {
    '401': {'description': 'Authentication required', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '403': {'description': "Your plan is below this operation's x-min-tier", 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '429': {'description': 'Rate limit exceeded'},
}


def get_paths():
    return {
        '/api/ai/chat': {
            'post': {
                'tags': [_TAG],
                'summary': 'Send a message to the AI copilot',
                'description': (
                    'Server-Sent Events (SSE) stream. The AI assembles context from '
                    'chart data, scanner scores, plays, and drawings before responding. '
                    'Each SSE event is a JSON object: `{ctx: ...}` for context summary, '
                    '`{t: ...}` for text chunks, `{done: true, charged: N}` for completion.'
                ),
                'operationId': 'aiChat',
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'required': ['message'],
                        'properties': {
                            'message': {'type': 'string', 'description': 'User message'},
                            'conversation_id': {'type': 'integer', 'description': 'Existing conversation (omit for new)'},
                            'asset': {'type': 'string', 'description': 'Asset context'},
                            'timeframe': {'type': 'string'},
                            'page_context': {'type': 'string', 'enum': ['chart', 'scanner', 'journal', 'report']},
                        },
                    }}},
                },
                'responses': {
                    '200': {
                        'description': 'SSE stream of AI response chunks',
                        'content': {'text/event-stream': {'schema': {'type': 'string'}}},
                    },
                    '400': {'description': 'Missing required fields'},
                    **_ERR,
                },
            },
        },
        '/api/ai/balance': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get AI credit balance',
                'operationId': 'getAIBalance',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Current credit balance',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'balance_cents': {'type': 'integer'},
                                'balance': {'type': 'string', 'example': '$5.00'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/ai/conversations': {
            'get': {
                'tags': [_TAG],
                'summary': 'List AI conversations',
                'operationId': 'listConversations',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'List of conversations',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'conversations': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'id': {'type': 'integer'},
                                            'title': {'type': 'string'},
                                            'primary_asset': {'type': 'string'},
                                            'page_context': {'type': 'string'},
                                            'message_count': {'type': 'integer'},
                                            'created_at': {'type': 'string', 'format': 'date-time'},
                                            'updated_at': {'type': 'string', 'format': 'date-time'},
                                        },
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
                'summary': 'Create a new conversation',
                'operationId': 'createConversation',
                'security': _AUTH,
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': {
                        'type': 'object',
                        'properties': {
                            'title': {'type': 'string'},
                            'primary_asset': {'type': 'string'},
                            'page_context': {'type': 'string'},
                        },
                    }}},
                },
                'responses': {
                    '201': {
                        'description': 'Conversation created',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {'id': {'type': 'integer'}},
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/ai/quick-score': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get AI quick score for an asset',
                'description': 'Fast AI-generated scoring without full conversation context.',
                'operationId': 'getQuickScore',
                'parameters': [
                    {'name': 'asset', 'in': 'query', 'required': True, 'schema': {'type': 'string'}},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Quick score result'},
                    **_ERR,
                },
            },
        },

        # ── Conversation + message management ──────────────────────────
        '/api/ai/conversations/{conv_id}/messages': {
            'get': {
                'tags': [_TAG],
                'summary': 'List messages in a conversation',
                'operationId': 'listConversationMessages',
                'security': _AUTH,
                'parameters': [{'name': 'conv_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                'responses': {'200': {'description': 'Message list'}, **_ERR},
            },
        },
        '/api/ai/conversations/{conv_id}': {
            'delete': {
                'tags': [_TAG],
                'summary': 'Delete a conversation and its messages',
                'operationId': 'deleteConversation',
                'security': _AUTH,
                'parameters': [{'name': 'conv_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                'responses': {'200': {'description': 'Deleted'}, **_ERR},
            },
        },

        # ── Assets + context ───────────────────────────────────────────
        '/api/ai/assets': {
            'get': {
                'tags': [_TAG],
                'summary': 'List assets available to the AI copilot',
                'operationId': 'listAIAssets',
                'security': _AUTH,
                'responses': {'200': {'description': 'Asset metadata'}, **_ERR},
            },
        },
        '/api/ai/context-blocks': {
            'get': {
                'tags': [_TAG],
                'summary': 'Inspect which context blocks would ship with the next chat',
                'description': 'Transparent view of the tiered-context assembly (always-on, per-asset, per-feature).',
                'operationId': 'getContextBlocks',
                'security': _AUTH,
                'responses': {'200': {'description': 'Context block summaries'}, **_ERR},
            },
        },
        '/api/ai/context-preview': {
            'post': {
                'tags': [_TAG],
                'summary': 'Dry-run context assembly for an asset/feature combination',
                'description': 'Returns the exact prompt + token count that would be sent to the LLM.',
                'operationId': 'previewContext',
                'security': _AUTH,
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object',
                    'properties': {
                        'asset': {'type': 'string'},
                        'feature': {'type': 'string'},
                        'timeframe': {'type': 'string'},
                    },
                }}}},
                'responses': {'200': {'description': 'Assembled prompt + token count'}, **_ERR},
            },
        },

        # ── Tool actions + pattern match ───────────────────────────────
        '/api/ai/execute-action': {
            'post': {
                'tags': [_TAG],
                'summary': 'Execute a confirmed write tool call from the copilot',
                'description': 'Rate-limited (20/minute) — runs a pre-registered write action with the caller\'s user_id.',
                'operationId': 'executeAIAction',
                'security': _AUTH,
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object',
                    'required': ['tool', 'arguments'],
                    'properties': {
                        'tool': {'type': 'string'},
                        'arguments': {'type': 'object'},
                    },
                }}}},
                'responses': {
                    '200': {'description': 'Action result'},
                    '429': {'description': 'Rate limit exceeded (20/min)'},
                    **_ERR,
                },
            },
        },
        '/api/ai/pattern-match': {
            'post': {
                'tags': [_TAG],
                'summary': 'Find historical chart analogues for a reference setup',
                'operationId': 'aiPatternMatch',
                'security': _AUTH,
                'requestBody': {'required': True, 'content': {'application/json': {'schema': {
                    'type': 'object',
                    'properties': {
                        'asset': {'type': 'string'},
                        'timeframe': {'type': 'string'},
                    },
                }}}},
                'responses': {'200': {'description': 'Analogue matches'}, **_ERR},
            },
        },

        # ── Trading journal ────────────────────────────────────────────
        '/api/ai/journal': {
            'get': {
                'tags': [_TAG],
                'summary': 'List AI-reviewed journal entries',
                'operationId': 'listAIJournal',
                'security': _AUTH,
                'responses': {'200': {'description': 'Journal entries'}, **_ERR},
            },
        },
        '/api/ai/journal/asset-history': {
            'get': {
                'tags': [_TAG],
                'summary': 'Journal history filtered to a specific asset',
                'operationId': 'journalAssetHistory',
                'security': _AUTH,
                'parameters': [
                    {'name': 'asset', 'in': 'query', 'required': True, 'schema': {'type': 'string'}},
                ],
                'responses': {'200': {'description': 'Asset history'}, **_ERR},
            },
        },
        '/api/ai/journal/analytics': {
            'get': {
                'tags': [_TAG],
                'summary': 'AI-computed journal analytics (win rate, R multiples, etc.)',
                'operationId': 'journalAnalytics',
                'security': _AUTH,
                'responses': {'200': {'description': 'Analytics summary'}, **_ERR},
            },
        },
        '/api/ai/journal/open': {
            'post': {
                'tags': [_TAG],
                'summary': 'Open a new journal entry from an AI suggestion',
                'operationId': 'openJournalEntry',
                'security': _AUTH,
                'responses': {'200': {'description': 'Entry opened'}, **_ERR},
            },
        },
        '/api/ai/journal/close': {
            'post': {
                'tags': [_TAG],
                'summary': 'Close an open journal entry (with outcome)',
                'operationId': 'closeJournalEntry',
                'security': _AUTH,
                'responses': {'200': {'description': 'Entry closed'}, **_ERR},
            },
        },
        '/api/ai/journal/{journal_id}': {
            'patch': {
                'tags': [_TAG],
                'summary': 'Patch a journal entry',
                'operationId': 'patchJournalEntry',
                'security': _AUTH,
                'parameters': [{'name': 'journal_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                'responses': {'200': {'description': 'Patched'}, **_ERR},
            },
        },
        '/api/ai/journal/{journal_id}/reopen': {
            'post': {
                'tags': [_TAG],
                'summary': 'Reopen a closed journal entry',
                'operationId': 'reopenJournalEntry',
                'security': _AUTH,
                'parameters': [{'name': 'journal_id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                'responses': {'200': {'description': 'Reopened'}, **_ERR},
            },
        },
        '/api/ai/journal/import': {
            'post': {
                'tags': [_TAG],
                'summary': 'Import journal entries (CSV or JSON payload)',
                'operationId': 'importJournal',
                'security': _AUTH,
                'responses': {'200': {'description': 'Import summary'}, **_ERR},
            },
        },

        # ── Trading profile + weekly review ────────────────────────────
        '/api/ai/trading-profile': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get the user\'s stored trading profile (style, risk, assets)',
                'operationId': 'getTradingProfile',
                'security': _AUTH,
                'responses': {'200': {'description': 'Trading profile'}, **_ERR},
            },
            'post': {
                'tags': [_TAG],
                'summary': 'Update the user\'s trading profile',
                'operationId': 'saveTradingProfile',
                'security': _AUTH,
                'responses': {'200': {'description': 'Saved'}, **_ERR},
            },
        },
        '/api/ai/weekly-review': {
            'post': {
                'tags': [_TAG],
                'summary': 'Generate the weekly AI review for the signed-in user',
                'operationId': 'generateWeeklyReview',
                'security': _AUTH,
                'responses': {'200': {'description': 'Weekly review body + charged credits'}, **_ERR},
            },
        },

        # ── Recipes (reusable prompt templates) ────────────────────────
        '/api/ai/recipes': {
            'get': {
                'tags': [_TAG],
                'summary': 'List the user\'s AI recipes',
                'operationId': 'listRecipes',
                'security': _AUTH,
                'responses': {'200': {'description': 'Recipe list'}, **_ERR},
            },
            'post': {
                'tags': [_TAG],
                'summary': 'Create a new AI recipe',
                'operationId': 'createRecipe',
                'security': _AUTH,
                'responses': {'201': {'description': 'Recipe created'}, **_ERR},
            },
        },
        '/api/ai/recipes/{rid}': {
            'put': {
                'tags': [_TAG],
                'summary': 'Update an AI recipe',
                'operationId': 'updateRecipe',
                'security': _AUTH,
                'parameters': [{'name': 'rid', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                'responses': {'200': {'description': 'Recipe updated'}, **_ERR},
            },
            'delete': {
                'tags': [_TAG],
                'summary': 'Delete an AI recipe',
                'operationId': 'deleteRecipe',
                'security': _AUTH,
                'parameters': [{'name': 'rid', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}],
                'responses': {'200': {'description': 'Recipe deleted'}, **_ERR},
            },
        },
        '/api/ai/recipes/templates': {
            'get': {
                'tags': [_TAG],
                'summary': 'List built-in recipe templates',
                'operationId': 'listRecipeTemplates',
                'security': _AUTH,
                'responses': {'200': {'description': 'Template list'}, **_ERR},
            },
        },
    }
