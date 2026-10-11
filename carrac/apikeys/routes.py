"""API key management routes — CRUD for per-user API keys."""

import logging

from flask import jsonify, request, session

from carrac.apikeys import apikeys_bp
from carrac.apikeys.auth import (
    generate_api_key, hash_api_key, get_key_suffix, MAX_KEYS_PER_USER,
)
from carrac.auth.decorators import login_required
from carrac.openapi.spec_decorator import spec
from carrac.rate_pools import API_ALLOWED_TIERS

logger = logging.getLogger('carrac.apikeys')


@apikeys_bp.route('/api/keys', methods=['GET'])
@login_required
@spec(
    summary='List API keys',
    description='Masked listing of the caller\'s API keys (full key is only returned at create time).',
    tags=['API Keys'],
    responses={200: 'ApiKeyList'},
    min_tier='free',
)
def list_keys():
    """List user's API keys (masked)."""
    from user_db import user_db
    keys = user_db.list_api_keys(session['user_id'])
    return jsonify({'keys': keys})


@apikeys_bp.route('/api/keys', methods=['POST'])
@login_required
@spec(
    summary='Create API key',
    description='Issue a new per-user API key. Full key is returned ONCE — save it.',
    tags=['API Keys'],
    request_body='ApiKeyCreate',
    responses={201: 'ApiKeyCreated', 400: 'Error', 403: 'Error'},
    min_tier='free',
)
def create_key():
    """Create a new API key. Returns the full key ONCE."""
    from user_db import user_db

    user_id = session['user_id']
    tier = session.get('tier', 'free')

    # Tier gate: open to every real tier (free invite-only beta). Only an
    # unknown/disabled tier is refused — paid value is enforced per-tool.
    if tier not in API_ALLOWED_TIERS:
        return jsonify({
            'error': 'API access is not available for this account.',
        }), 403

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or 'Default')[:50]
    scope = data.get('scope', 'full')
    if scope not in ('full', 'read-only'):
        scope = 'full'

    # Check limit
    existing = user_db.list_api_keys(user_id)
    active = [k for k in existing if not k.get('revoked')]
    if len(active) >= MAX_KEYS_PER_USER:
        return jsonify({
            'error': f'Maximum {MAX_KEYS_PER_USER} active API keys allowed',
        }), 400

    key = generate_api_key()
    key_h = hash_api_key(key)
    suffix = get_key_suffix(key)

    key_id = user_db.create_api_key(user_id, key_h, suffix, name, scope)
    if not key_id:
        return jsonify({'error': 'Failed to create API key'}), 500

    logger.info("API key created for user %s: ...%s", user_id, suffix)

    return jsonify({
        'id': key_id,
        'key': key,  # Full key — shown once, never again
        'suffix': suffix,
        'name': name,
        'scope': scope,
    }), 201


@apikeys_bp.route('/api/keys/<int:key_id>', methods=['DELETE'])
@login_required
@spec(
    summary='Revoke API key',
    tags=['API Keys'],
    responses={200: {'description': 'Revoked'}, 404: 'Error'},
    min_tier='free',
)
def revoke_key(key_id):
    """Revoke an API key."""
    from user_db import user_db
    success = user_db.revoke_api_key(key_id, session['user_id'])
    if not success:
        return jsonify({'error': 'API key not found'}), 404
    logger.info("API key %d revoked by user %s", key_id, session['user_id'])
    return jsonify({'success': True})
