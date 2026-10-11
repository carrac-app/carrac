"""OpenAPI blueprint: Swagger UI + spec endpoint.

Both routes are PUBLIC (no auth). The spec is the contract —
anyone should be able to inspect it to decide whether to integrate.
Rate-limited only via the shared flask-limiter pool (not per-user), and
the REST sliding-window limiter explicitly whitelists these paths.
"""

from flask import Blueprint, jsonify, render_template

openapi_bp = Blueprint('openapi', __name__)


@openapi_bp.route('/api/docs')
def swagger_ui():
    """Swagger UI page for interactive API exploration (public)."""
    return render_template('api_docs.html')


@openapi_bp.route('/api/openapi.json')
def openapi_json():
    """Full OpenAPI 3.0 spec (public contract — no auth required)."""
    from . import build_spec
    return jsonify(build_spec())
