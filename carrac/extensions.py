"""
Flask extensions — initialized here, bound to app in create_app().

This avoids circular imports: blueprints import extensions from here,
the app factory binds them to the Flask instance.
"""

from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

csrf = CSRFProtect()


def _rate_limit_key():
    """Pool-aware rate limit key.

    Uses the pool-namespaced key set by rate_pools.classify_request()
    in before_request. Falls back to user:id or IP for requests before
    the classifier runs (e.g. during app startup or rate limiter init).
    """
    from flask import g, session
    if hasattr(g, 'rate_key'):
        return g.rate_key
    user_id = session.get('user_id')
    if user_id:
        return f"user:{user_id}"
    return get_remote_address()


limiter = Limiter(key_func=_rate_limit_key)
