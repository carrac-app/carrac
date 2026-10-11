"""
Historical Context API

Flask blueprint for morning bot historical data operations.
Returns compact historical sentiment context.

There is deliberately no write path here (no POST /snapshot, no /patterns
for options_history / heatmap_history / pattern_log): those tables had no
live writer. The sentiment write is covered by carrac/precompute.py, which
writes sentiment_history independently on the worker schedule, so a POST
route here would be a zombie with no real backing. GET /context
(sentiment-only) is the only route.
"""

from flask import Blueprint, request, jsonify

from carrac.content_db import get_content_db
from carrac.drawings.routes import handle_errors, login_required_api

history_bp = Blueprint('history', __name__, url_prefix='/api/history')


# =============================================================================
# Endpoints
# =============================================================================

@history_bp.route('/context', methods=['GET'])
@login_required_api
@handle_errors
def get_context():
    """GET /api/history/context?days=14
    Returns compact historical context for morning bot prompt."""
    days = request.args.get('days', 14, type=int)
    days = min(max(days, 1), 30)  # clamp 1-30

    db = get_content_db()
    context = db.build_history_context(days=days)

    return jsonify({
        "success": True,
        **context
    })
