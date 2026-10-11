"""
Widget blueprint — token management, widget API, setup page, downloads.
"""

from flask import Blueprint

widget_bp = Blueprint('widget', __name__)

from carrac.widget import routes  # noqa: E402, F401
