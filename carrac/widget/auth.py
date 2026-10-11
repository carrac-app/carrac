"""
Widget token security — generation, hashing, verification, rate limiting.

Tokens use SHA-256 hashing for storage (not bcrypt — tokens have 128 bits of
entropy so brute-force is infeasible; fast lookup is more important).
"""

import hashlib
import secrets
import time
import threading
import logging
from functools import wraps

from flask import request, jsonify

logger = logging.getLogger('carrac.widget')

TOKEN_PREFIX = 'cvx_w_'
MAX_TOKENS_PER_USER = 5
RATE_LIMIT_MAX = 200
RATE_LIMIT_WINDOW = 3600  # 1 hour

# In-memory sliding-window rate limiter: {token_hash: [timestamps]}
_rate_buckets = {}
_rate_lock = threading.Lock()


def generate_token() -> str:
    """Generate a new widget token: cvx_w_ + 32 hex chars (128 bits)."""
    return TOKEN_PREFIX + secrets.token_hex(16)


def hash_token(token: str) -> str:
    """SHA-256 hash a widget token for DB storage."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def get_token_suffix(token: str) -> str:
    """Last 4 characters for masked display."""
    return token[-4:]


def validate_token_format(token: str) -> bool:
    """Check token has valid prefix + 32 hex chars."""
    if not token or not isinstance(token, str):
        return False
    if not token.startswith(TOKEN_PREFIX):
        return False
    hex_part = token[len(TOKEN_PREFIX):]
    if len(hex_part) != 32:
        return False
    try:
        int(hex_part, 16)
        return True
    except ValueError:
        return False


def check_rate_limit(token_hash: str) -> bool:
    """Sliding-window rate limiter. Returns True if request is allowed."""
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    with _rate_lock:
        bucket = _rate_buckets.get(token_hash)
        if bucket is None:
            _rate_buckets[token_hash] = [now]
            return True
        # Prune expired timestamps
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= RATE_LIMIT_MAX:
            return False
        bucket.append(now)
        return True


def extract_token_from_request() -> str:
    """Extract widget token from Authorization header or ?token= query param."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:].strip()
    return request.args.get('token', '').strip()


def widget_token_required(f):
    """Decorator: require valid widget token. Sets request.widget_* attributes."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = extract_token_from_request()

        if not token or not validate_token_format(token):
            return jsonify({'error': 'Authentication required'}), 401

        token_h = hash_token(token)

        # Rate limit
        if not check_rate_limit(token_h):
            return jsonify({'error': 'Rate limit exceeded', 'retry_after': 60}), 429

        # DB verification
        from user_db import user_db
        token_info = user_db.verify_widget_token(token_h)
        if not token_info:
            # Generic error — don't reveal if token exists but is revoked
            return jsonify({'error': 'Authentication required'}), 401

        request.widget_user_id = token_info['user_id']
        request.widget_token_id = token_info['id']
        request.widget_user_tier = token_info.get('tier', 'free')

        return f(*args, **kwargs)
    return decorated
