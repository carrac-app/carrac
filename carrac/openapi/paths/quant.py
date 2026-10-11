"""Quant DB endpoints."""

_AUTH = [{'api_key': []}, {'session_cookie': []}]
_TAG = 'Quant'
_ERR = {
    '401': {'description': 'Authentication required', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '403': {'description': 'Paid subscription required (member tier and up — quant_engine feature)', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}},
    '429': {'description': 'Rate limit exceeded'},
}


def get_paths():
    return {
        '/api/quant/health': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get Quant DB health status',
                'description': 'Collector statuses, data freshness, and completeness metrics.',
                'operationId': 'getQuantHealth',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Health status',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'status': {'type': 'string'},
                                'collectors': {'type': 'array', 'items': {'type': 'object'}},
                                'freshness': {'type': 'array', 'items': {'type': 'object'}},
                                'completeness': {'type': 'object'},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/quant/metrics': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get available metrics',
                'description': 'Lists all tracked metrics grouped by domain (macro, price, derivatives, sentiment).',
                'operationId': 'getQuantMetrics',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Available metrics by domain',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'macro': {'type': 'array', 'items': {'type': 'object'}},
                                'price': {'type': 'array', 'items': {'type': 'object'}},
                                'derivatives': {'type': 'array', 'items': {'type': 'object'}},
                                'sentiment': {'type': 'array', 'items': {'type': 'object'}},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/quant/zscores': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get current z-scores',
                'description': 'Statistical z-scores and percentiles for all tracked metrics.',
                'operationId': 'getQuantZscores',
                'parameters': [
                    {'name': 'window', 'in': 'query', 'required': False,
                     'schema': {'type': 'integer', 'default': 90},
                     'description': 'Lookback window in days'},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Z-scores with percentiles',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'window': {'type': 'integer'},
                                'count': {'type': 'integer'},
                                'data': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'domain': {'type': 'string'},
                                            'metric': {'type': 'string'},
                                            'zscore': {'type': 'number'},
                                            'percentile': {'type': 'number'},
                                            'raw_value': {'type': 'number'},
                                        },
                                    },
                                },
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/quant/regime': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get current market regime',
                'description': 'HMM-based regime detection: risk-on, risk-off, or neutral.',
                'operationId': 'getQuantRegime',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Current regime state',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'current': {
                                    'type': 'object',
                                    'properties': {
                                        'state': {'type': 'string'},
                                        'probability': {'type': 'number'},
                                        'duration_days': {'type': 'integer'},
                                        'since': {'type': 'string', 'format': 'date'},
                                    },
                                },
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/quant/snapshot': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get full quant snapshot',
                'description': (
                    'Comprehensive snapshot: latest macro values, anomalies, '
                    'regime state, top correlations, and lead-lag signals.'
                ),
                'operationId': 'getQuantSnapshot',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Full quant snapshot',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'macro': {'type': 'object'},
                                'sentiment': {'type': 'object'},
                                'anomalies': {'type': 'array', 'items': {'type': 'object'}},
                                'regime': {'type': 'object'},
                                'top_correlations': {'type': 'array', 'items': {'type': 'object'}},
                                'leadlag_signals': {'type': 'array', 'items': {'type': 'object'}},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/quant/correlations': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get cross-asset correlations',
                'operationId': 'getQuantCorrelations',
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Correlation matrix and top pairs'},
                    **_ERR,
                },
            },
        },
        '/api/quant/events': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get upcoming economic events',
                'operationId': 'getQuantEvents',
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Economic calendar events'},
                    **_ERR,
                },
            },
        },
        '/api/quant/derivatives-regime': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get derivatives regime classification',
                'description': (
                    'Deterministic regime classifier matching BTC/ETH derivatives '
                    'metrics (basis, skew, DVOL, IV/RV, GEX, put/call ratio, term '
                    'structure) against 12 canonical regimes. Returns regime name, '
                    'confidence, implications, and transition warnings.'
                ),
                'operationId': 'getDerivativesRegime',
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Derivatives regime with metrics snapshot',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'by_asset': {
                                    'type': 'object',
                                    'description': 'Per-asset derivatives metrics (BTC, ETH)',
                                },
                                'regime': {
                                    'type': 'object',
                                    'properties': {
                                        'regime': {'type': 'string', 'description': 'Regime key'},
                                        'regime_name': {'type': 'string'},
                                        'confidence': {'type': 'number'},
                                        'description': {'type': 'string'},
                                        'implications': {'type': 'array', 'items': {'type': 'string'}},
                                        'transitions': {'type': 'array', 'items': {'type': 'string'}},
                                        'secondary': {'type': 'string'},
                                        'secondary_confidence': {'type': 'number'},
                                    },
                                },
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/quant/leadlag': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get lead-lag relationships',
                'description': (
                    'Which macro/sentiment metrics lead a target asset. '
                    'Returns optimal lag, correlation strength, and persistence.'
                ),
                'operationId': 'getQuantLeadlag',
                'parameters': [
                    {'name': 'target', 'in': 'query', 'required': True,
                     'schema': {'type': 'string'},
                     'description': 'Target asset (e.g. BTC, ETH, SPX)'},
                    {'name': 'frequency', 'in': 'query', 'required': False,
                     'schema': {'type': 'string', 'enum': ['daily', 'weekly'], 'default': 'daily'},
                     'description': 'Analysis frequency'},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Lead-lag signals sorted by |correlation|',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'target': {'type': 'string'},
                                'count': {'type': 'integer'},
                                'data': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'leader': {'type': 'string'},
                                            'best_lag': {'type': 'integer'},
                                            'best_correlation': {'type': 'number'},
                                            'persistent': {'type': 'boolean'},
                                        },
                                    },
                                },
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/quant/chain': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get transmission chain analysis',
                'description': (
                    'Macro transmission chains showing how forces propagate to '
                    'crypto: monetary_policy, dollar_liquidity, risk_appetite. '
                    'Each link shows lag and correlation; aggregate shows chain '
                    'strength (geometric mean of |r|) and weakest link.'
                ),
                'operationId': 'getQuantChain',
                'parameters': [
                    {'name': 'chain', 'in': 'query', 'required': True,
                     'schema': {'type': 'string', 'enum': ['monetary_policy', 'dollar_liquidity', 'risk_appetite']},
                     'description': 'Which transmission chain to analyze'},
                    {'name': 'frequency', 'in': 'query', 'required': False,
                     'schema': {'type': 'string', 'enum': ['daily', 'weekly'], 'default': 'daily'},
                     'description': 'Analysis frequency'},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Chain links with correlations and aggregate strength',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'chain': {'type': 'string'},
                                'chain_strength': {'type': 'number'},
                                'weakest_link': {'type': 'object'},
                                'links': {'type': 'array', 'items': {'type': 'object'}},
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/quant/analogues': {
            'get': {
                'tags': [_TAG],
                'summary': 'Find historical analogues',
                'description': (
                    'Search for past dates most similar to today using cosine '
                    'similarity across z-score vectors (macro, sentiment, '
                    'derivatives). Shows forward returns for each analogue. '
                    'Each analogue\'s similarity is computed only over the '
                    'metrics common to both the reference date and that '
                    'historical date, so the metric basis varies per entry — '
                    'see similarity_basis (top-level) and basis/n_common_metrics '
                    '(per analogue) in the response.'
                ),
                'operationId': 'getQuantAnalogues',
                'parameters': [
                    {'name': 'target', 'in': 'query', 'required': False,
                     'schema': {'type': 'string', 'default': 'BTC'},
                     'description': 'Asset for forward returns'},
                    {'name': 'top', 'in': 'query', 'required': False,
                     'schema': {'type': 'integer', 'default': 5},
                     'description': 'Number of analogues to return'},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {
                        'description': 'Historical analogues with forward returns',
                        'content': {'application/json': {'schema': {
                            'type': 'object',
                            'properties': {
                                'reference_date': {'type': 'string'},
                                'target': {'type': 'string'},
                                'current_vector': {'type': 'object'},
                                'n_metrics': {'type': 'integer'},
                                'similarity_basis': {
                                    'type': 'object',
                                    'description': (
                                        'Discloses the varying per-analogue metric '
                                        'basis: analogue_metrics_total, '
                                        'reference_metrics_available, '
                                        'min_common_required, note.'
                                    ),
                                    'properties': {
                                        'analogue_metrics_total': {'type': 'integer'},
                                        'reference_metrics_available': {'type': 'integer'},
                                        'min_common_required': {'type': 'integer'},
                                        'note': {'type': 'string'},
                                    },
                                },
                                'analogues': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'date': {'type': 'string'},
                                            'similarity': {'type': 'number'},
                                            'vector': {'type': 'object'},
                                            'forward_return': {'type': 'object'},
                                            'n_common_metrics': {'type': 'integer'},
                                            'basis': {
                                                'type': 'string',
                                                'description': (
                                                    'n_common_metrics/analogue_metrics_total, '
                                                    'e.g. "6/10" — for verbatim quoting '
                                                    'alongside a similarity %.'
                                                ),
                                            },
                                        },
                                    },
                                },
                            },
                        }}},
                    },
                    **_ERR,
                },
            },
        },
        '/api/quant/conditional': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get conditional return distributions',
                'description': (
                    'When z-score conditions held simultaneously in the past, '
                    'what was the N-day forward return distribution?'
                ),
                'operationId': 'getQuantConditional',
                'parameters': [
                    {'name': 'conditions', 'in': 'query', 'required': True,
                     'schema': {'type': 'string'},
                     'description': 'Comma-separated conditions (e.g. VIX:gt:2,HY_SPREAD:gt:1.5)'},
                    {'name': 'target', 'in': 'query', 'required': False,
                     'schema': {'type': 'string', 'default': 'BTC'},
                     'description': 'Asset for forward returns'},
                    {'name': 'horizon', 'in': 'query', 'required': False,
                     'schema': {'type': 'integer', 'default': 30},
                     'description': 'Forward return horizon in days'},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Conditional return distribution'},
                    **_ERR,
                },
            },
        },
        '/api/quant/anomalies': {
            'get': {
                'tags': [_TAG],
                'summary': 'Get historical anomaly clusters',
                'description': 'Multi-domain anomaly clusters with metrics and resolution status.',
                'operationId': 'getQuantAnomalies',
                'parameters': [
                    {'name': 'limit', 'in': 'query', 'required': False,
                     'schema': {'type': 'integer', 'default': 30},
                     'description': 'Max rows to return'},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Anomaly cluster history'},
                    **_ERR,
                },
            },
        },
        '/api/quant/pivot': {
            'get': {
                'tags': [_TAG],
                'summary': 'Pivot z-score aggregation',
                'description': (
                    'Aggregate z-scores across metrics with grouping by regime, '
                    'z-score bucket, month, or quarter.'
                ),
                'operationId': 'getQuantPivot',
                'parameters': [
                    {'name': 'metrics', 'in': 'query', 'required': True,
                     'schema': {'type': 'string'},
                     'description': 'Comma-separated canonical metric names'},
                    {'name': 'group_by', 'in': 'query', 'required': False,
                     'schema': {'type': 'string', 'enum': ['regime', 'zscore_bucket', 'monthly', 'quarterly']},
                     'description': 'Grouping dimension'},
                ],
                'security': _AUTH,
                'responses': {
                    '200': {'description': 'Aggregated z-score pivot table'},
                    **_ERR,
                },
            },
        },
    }
