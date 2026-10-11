"""Flask JSON responses with ISO 8601 dates."""

from datetime import date, datetime

from flask import current_app
from flask.json.provider import DefaultJSONProvider


def _iso_default(obj):
    """JSON fallback: dates as ISO 8601, everything else as Flask does."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return DefaultJSONProvider.default(obj)


def jsonify_iso(payload):
    """``jsonify`` with ISO 8601 dates.

    Flask serialises a ``datetime`` as RFC 1123 (``Sat, 10 Oct 2026 ...``),
    while the FastAPI endpoints return ISO 8601. Routing a Flask response
    through this keeps one date format across both runtimes.
    """
    text = current_app.json.dumps(payload, default=_iso_default)
    return current_app.response_class(text + '\n', mimetype=current_app.json.mimetype)
