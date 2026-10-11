"""API keys blueprint — per-user key management for REST API and MCP."""

from flask import Blueprint

apikeys_bp = Blueprint('apikeys', __name__)

from carrac.apikeys import routes  # noqa: E402, F401
