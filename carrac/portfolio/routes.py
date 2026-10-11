"""The /portfolio page route.

Auth mirrors /journal (carrac/journal/routes.py::journal_page): @login_required with
no tier gate.
"""

from flask import Blueprint, render_template

from carrac.auth.decorators import get_current_user, login_required

portfolio_bp = Blueprint('portfolio', __name__)


@portfolio_bp.route('/portfolio')
@login_required
def portfolio_page():
    """Dedicated portfolio page."""
    return render_template('portfolio.html', user=get_current_user())
