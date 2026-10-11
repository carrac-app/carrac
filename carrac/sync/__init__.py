"""
Real-time sync module — SSE streaming for cross-device drawing sync.
"""

from flask import Blueprint

sync_bp = Blueprint('sync', __name__, url_prefix='/api/sync')
