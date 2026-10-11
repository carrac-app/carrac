"""
API key generation, hashing, and validation.

Mirrors carrac/widget/auth.py, with a cvx_k_ prefix for API keys.
Tokens use SHA-256 hashing (128-bit entropy makes brute-force infeasible).
"""

import hashlib
import secrets

TOKEN_PREFIX = 'cvx_k_'
MAX_KEYS_PER_USER = 5


def generate_api_key():
    """Generate a new API key: cvx_k_ + 32 hex chars (128 bits)."""
    return TOKEN_PREFIX + secrets.token_hex(16)


def hash_api_key(key):
    """SHA-256 hash an API key for DB storage."""
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def get_key_suffix(key):
    """Last 4 characters for masked display."""
    return key[-4:]


def validate_key_format(key):
    """Check key has valid prefix + 32 hex chars."""
    if not key or not isinstance(key, str):
        return False
    if not key.startswith(TOKEN_PREFIX):
        return False
    hex_part = key[len(TOKEN_PREFIX):]
    if len(hex_part) != 32:
        return False
    try:
        int(hex_part, 16)
        return True
    except ValueError:
        return False
