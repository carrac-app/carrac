"""
Legal pages: Terms of Service, Privacy Policy, Financial Disclaimer.
"""

from flask import Blueprint, render_template

legal_bp = Blueprint('legal', __name__)


@legal_bp.route('/terms')
def terms():
    return render_template('terms.html')


@legal_bp.route('/privacy')
def privacy():
    return render_template('privacy.html')


@legal_bp.route('/disclaimer')
def disclaimer():
    return render_template('disclaimer.html')



@legal_bp.route('/contact')
def contact():
    return render_template('contact.html')
