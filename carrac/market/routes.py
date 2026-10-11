"""
Market analytics routes.

GET /api/market/breadth  — RSI/SMA50/20D-high-low breadth across all tracked assets.
"""

import logging

from flask import Blueprint, jsonify, request

from carrac.auth.decorators import auth_or_key_required

# Import at module level so tests can monkeypatch 'carrac.market.routes.get_breadth'.
# The actual computation module is only loaded when this blueprint is first imported
# (lazy import is preserved — the module-level name here is just the handle).
from carrac.market.breadth import get_breadth  # noqa: E402

logger = logging.getLogger('carrac.market.routes')

market_bp = Blueprint('market', __name__)


@market_bp.route('/api/market/breadth')
@auth_or_key_required
def get_market_breadth():
    """Return market breadth statistics across all tracked assets.

    Counts and percentages for:
      - RSI(14) daily oversold (<30) / overbought (>70)
      - Price above/below SMA(50)
      - New 20-day highs / lows
      - A one-word breadth label

    Cached 1 hour (breadth moves slowly on daily indicators).
    Pass ?refresh=1 (admin only) to force a recompute.
    """
    from flask import session

    force = request.args.get('refresh') == '1' and session.get('is_admin')

    try:
        result = get_breadth(force_refresh=bool(force))
    except Exception as e:
        logger.error(f'breadth endpoint error: {e}', exc_info=True)
        return jsonify({'error': 'breadth computation failed', 'detail': str(e)}), 503

    return jsonify(result)
