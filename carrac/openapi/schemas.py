"""Reusable OpenAPI component schemas."""


def get_component_schemas():
    """Return shared schema definitions referenced by $ref in paths."""
    return {
        'Error': {
            'type': 'object',
            'required': ['error'],
            'properties': {
                'error': {'type': 'string', 'description': 'Human-readable error message'},
                'upgrade_required': {'type': 'boolean', 'description': 'True when a higher tier is needed'},
                'current_tier': {'type': 'string'},
                'required_tiers': {'type': 'array', 'items': {'type': 'string'}},
            },
        },
        'OHLCV': {
            'type': 'object',
            'properties': {
                'time': {'type': 'integer', 'description': 'Unix timestamp (seconds)'},
                'open': {'type': 'number'},
                'high': {'type': 'number'},
                'low': {'type': 'number'},
                'close': {'type': 'number'},
            },
        },
        'Volume': {
            'type': 'object',
            'properties': {
                'time': {'type': 'integer'},
                'value': {'type': 'number'},
                'color': {'type': 'string'},
            },
        },
        'CPR': {
            'type': 'object',
            'description': 'Central Pivot Range with support/resistance levels',
            'properties': {
                'pivot': {'type': 'number'},
                'tc': {'type': 'number', 'description': 'Top Central'},
                'bc': {'type': 'number', 'description': 'Bottom Central'},
                'r1': {'type': 'number'}, 'r2': {'type': 'number'}, 'r3': {'type': 'number'},
                's1': {'type': 'number'}, 's2': {'type': 'number'}, 's3': {'type': 'number'},
                'prevHigh': {'type': 'number'},
                'prevLow': {'type': 'number'},
                'prevClose': {'type': 'number'},
                'periodStart': {'type': 'integer'},
                'history': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'pivot': {'type': 'number'},
                            'tc': {'type': 'number'},
                            'bc': {'type': 'number'},
                            'periodStart': {'type': 'integer'},
                            'periodEnd': {'type': 'integer'},
                        },
                    },
                },
            },
        },
        'Play': _play_schema(),
        'Alert': {
            'type': 'object',
            'properties': {
                'id': {'type': 'string'},
                'symbol': {'type': 'string'},
                'alert_type': {'type': 'string', 'enum': ['price', 'indicator', 'trendline']},
                'condition': {'type': 'string', 'enum': ['above', 'below', 'cross_above', 'cross_below']},
                'price': {'type': 'number'},
                'indicator': {'type': 'string', 'description': 'Indicator name (for indicator alerts)'},
                'target_state': {'description': 'Target indicator state'},
                'note': {'type': 'string'},
                'frequency': {'type': 'string', 'enum': ['once', 'once_per_bar']},
                'active': {'type': 'boolean'},
                'triggered': {'type': 'boolean'},
                'triggered_price': {'type': 'number'},
                'triggered_at': {'type': 'string', 'format': 'date-time'},
                'created_at': {'type': 'string', 'format': 'date-time'},
            },
        },
        'Trade': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'asset': {'type': 'string'},
                'direction': {'type': 'string', 'enum': ['long', 'short']},
                'entry_price': {'type': 'number'},
                'exit_price': {'type': 'number'},
                'target_price': {'type': 'number'},
                'stop_price': {'type': 'number'},
                'setup_type': {'type': 'string'},
                'tags': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
                'mistake_tags': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True},
                'rr_actual': {'type': 'number'},
                'opened_at': {'type': 'string', 'format': 'date-time'},
                'closed_at': {'type': 'string', 'format': 'date-time'},
            },
        },
        'ScannerResult': {
            'type': 'object',
            'properties': {
                'symbol': {'type': 'string'},
                'price': {'type': 'number'},
                'best_verdict': {'type': 'string'},
                'long': {
                    'type': 'object',
                    'properties': {
                        'total': {'type': 'number'},
                        'verdict': {'type': 'string'},
                    },
                },
                'short': {
                    'type': 'object',
                    'properties': {
                        'total': {'type': 'number'},
                        'verdict': {'type': 'string'},
                    },
                },
            },
        },
        'Drawing': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'asset': {'type': 'string'},
                'timeframe': {'type': 'string'},
                'drawing_type': {'type': 'string', 'enum': [
                    'horizontal_line', 'zone', 'annotation', 'trendline', 'fibonacci', 'position',
                    'long_position', 'short_position', 'spot', 'scenario', 'liquidation_zone']},
                'price_level': {'type': 'number'},
                'label': {'type': 'string'},
                'color': {'type': 'string'},
                'metadata': {'type': 'object'},
                'created_at': {'type': 'string', 'format': 'date-time'},
                'updated_at': {'type': 'string', 'format': 'date-time'},
            },
        },
        # ── auto-gen @spec referenced schemas ──────────
        'PlayList': {
            'type': 'object',
            'properties': {
                'plays': {'type': 'array', 'items': {'$ref': '#/components/schemas/Play'}},
                'count': {'type': 'integer'},
            },
        },
        'PlayCreate': {
            'type': 'object',
            'required': ['title', 'type', 'primary_asset'],
            'properties': {
                'title': {'type': 'string'},
                'thesis': {'type': 'string'},
                'type': {'type': 'string'},
                'direction': {'type': 'string'},
                'primary_asset': {'type': 'string'},
                'related_assets': {'type': 'array', 'items': {'type': 'string'}},
                'conditions': {'type': 'object'},
                'invalidation': {'type': 'object'},
                'timeframe': {'type': 'string'},
                'expiry': {'type': 'string', 'format': 'date-time'},
                'action_plan': {'type': 'string'},
            },
        },
        'PlayUpdate': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string'},
                'thesis': {'type': 'string'},
                'conditions': {'type': 'object'},
                'invalidation': {'type': 'object'},
                'action_plan': {'type': 'string'},
                'related_assets': {'type': 'array', 'items': {'type': 'string'}},
            },
        },
        'AlertList': {
            'type': 'object',
            'properties': {
                'alerts': {'type': 'array', 'items': {'$ref': '#/components/schemas/Alert'}},
            },
        },
        'AlertCreate': {
            'type': 'object',
            'required': ['symbol', 'alert_type'],
            'properties': {
                'symbol': {'type': 'string'},
                'alert_type': {'type': 'string', 'enum': ['price', 'indicator']},
                'condition': {'type': 'string'},
                'price': {'type': 'number'},
                'indicator': {'type': 'string'},
                'target_state': {'description': 'Target state (per indicator)'},
                'note': {'type': 'string'},
                'frequency': {'type': 'string', 'enum': ['once', 'once_per_bar']},
            },
        },
        'AssetDetail': {
            'type': 'object',
            'properties': {
                'symbol': {'type': 'string'},
                'market': {'type': 'object'},
                'scanner': {'type': 'object'},
                'regime': {'type': 'object'},
                'sentiment': {'type': 'object'},
                'plays': {'type': 'array', 'items': {'$ref': '#/components/schemas/Play'}},
                'alerts': {'type': 'array', 'items': {'$ref': '#/components/schemas/Alert'}},
                'drawings': {'type': 'object'},
                'positions': {'type': 'array', 'items': {'$ref': '#/components/schemas/Trade'}},
                'availability': {'type': 'object', 'description': 'Per-block ok|error|degraded status'},
            },
        },
        'PositionList': {
            'type': 'object',
            'properties': {
                'positions': {'type': 'array', 'items': {'$ref': '#/components/schemas/Trade'}},
                'count': {'type': 'integer'},
            },
        },
        'BatchResponse': {
            'type': 'object',
            'properties': {
                'assets': {
                    'type': 'object',
                    'additionalProperties': {'$ref': '#/components/schemas/AssetDetail'},
                },
                'blocks': {'type': 'array', 'items': {'type': 'string'}},
                'availability': {'type': 'object'},
            },
        },
        'ScannerResponse': {
            'type': 'object',
            'properties': {
                'results': {'type': 'array', 'items': {'$ref': '#/components/schemas/ScannerResult'}},
                'computed_at': {'type': 'string', 'format': 'date-time'},
            },
        },
        'ApiKey': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'name': {'type': 'string'},
                'suffix': {'type': 'string', 'description': 'Last 4 chars of the key for identification'},
                'scope': {'type': 'string', 'enum': ['full', 'read-only']},
                'created_at': {'type': 'string', 'format': 'date-time'},
                'last_used_at': {'type': 'string', 'format': 'date-time'},
                'revoked': {'type': 'boolean'},
            },
        },
        'ApiKeyList': {
            'type': 'object',
            'properties': {
                'keys': {'type': 'array', 'items': {'$ref': '#/components/schemas/ApiKey'}},
            },
        },
        'ApiKeyCreate': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'maxLength': 50},
                'scope': {'type': 'string', 'enum': ['full', 'read-only'], 'default': 'full'},
            },
        },
        'ApiKeyCreated': {
            'type': 'object',
            'required': ['id', 'key'],
            'properties': {
                'id': {'type': 'integer'},
                'key': {'type': 'string', 'description': 'Full API key — returned ONCE, store it.'},
                'suffix': {'type': 'string'},
                'name': {'type': 'string'},
                'scope': {'type': 'string'},
            },
        },
        'UsageStats': {
            'type': 'object',
            'properties': {
                'tier': {'type': 'string'},
                'unlimited': {'type': 'boolean'},
                'usage': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'window': {'type': 'string', 'enum': ['minute', 'hour', 'day']},
                            'window_seconds': {'type': 'integer'},
                            'used': {'type': 'integer'},
                            'limit': {'type': 'integer'},
                            'remaining': {'type': 'integer'},
                            'retry_after': {'type': 'integer'},
                        },
                    },
                },
            },
        },
    }


def _play_schema():
    """Build Play schema, importing live constants."""
    try:
        from carrac.plays.models import PLAY_TYPES, PLAY_STATUSES, PLAY_DIRECTIONS
        types = sorted(PLAY_TYPES)
        statuses = sorted(PLAY_STATUSES)
        directions = sorted(PLAY_DIRECTIONS)
    except ImportError:
        types = ['directional', 'event', 'macro']
        statuses = ['acting', 'completed', 'expired', 'invalidated', 'watching']
        directions = ['conditional', 'long', 'neutral', 'short']

    return {
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'title': {'type': 'string'},
            'thesis': {'type': 'string'},
            'type': {'type': 'string', 'enum': types},
            'direction': {'type': 'string', 'enum': directions},
            'status': {'type': 'string', 'enum': statuses},
            'primary_asset': {'type': 'string'},
            'related_assets': {'type': 'array', 'items': {'type': 'string'}},
            'conditions': {'type': 'object', 'description': 'Condition rules with evaluation state'},
            'invalidation': {'type': 'object', 'description': 'Invalidation criteria'},
            'timeframe': {'type': 'string'},
            'expiry': {'type': 'string', 'format': 'date-time'},
            'action_plan': {'type': 'string'},
            'scorer_snapshot': {'type': 'object'},
            'report_tracking': {'type': 'boolean'},
            'created_at': {'type': 'string', 'format': 'date-time'},
            'updated_at': {'type': 'string', 'format': 'date-time'},
        },
    }
