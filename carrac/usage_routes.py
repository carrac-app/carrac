"""Blueprint for /api/usage — self-introspection of API consumption."""

import logging

from flask import Blueprint, g, jsonify, request, session

from carrac.auth.decorators import auth_or_key_required, get_request_tier
from carrac.openapi.spec_decorator import spec
from carrac.usage import build_usage_response

logger = logging.getLogger('carrac.usage_routes')

usage_bp = Blueprint('usage', __name__)


@usage_bp.route('/api/usage', methods=['GET'])
@auth_or_key_required
@spec(
    summary='Your API usage',
    description='Returns your current rate-limit window consumption (minute/hour/day) '
                'for both REST and MCP surfaces, plus per-day call counts for the '
                'last N days. Counters never raise — reads without incrementing.',
    tags=['Usage'],
    responses={200: 'UsageStats', 401: 'Error'},
    parameters=[
        {'name': 'history', 'in': 'query',
         'schema': {'type': 'boolean', 'default': True}},
        {'name': 'days', 'in': 'query',
         'schema': {'type': 'integer', 'default': 30, 'minimum': 1, 'maximum': 90}},
    ],
    min_tier='member',
)
def get_usage():
    uid = getattr(g, 'api_key_user_id', None) or session.get('user_id')
    if uid is None:
        return jsonify({'error': 'Authentication required'}), 401

    tier = get_request_tier()
    include_history = request.args.get('history', 'true').lower() not in ('false', '0', 'no')
    try:
        days = max(1, min(int(request.args.get('days', 30)), 90))
    except (ValueError, TypeError):
        days = 30

    return jsonify(build_usage_response(uid, tier, include_history, days))
