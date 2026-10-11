"""
Sync poll endpoint for drawings and preferences.

Clients poll every ~5 seconds to check if drawings or preferences have changed
on the server. Returns timestamps and details so the client can decide what to
refresh.
"""

import logging

from flask import request, session, jsonify

from carrac.sync import sync_bp
from carrac.sync.events import get_last_change, get_last_pref_change

logger = logging.getLogger('carrac.sync')


@sync_bp.route('/poll', methods=['GET'])
def poll_drawings():
    """
    Lightweight poll endpoint for drawing and preference sync.

    Query params:
        since: Unix timestamp — client's last known drawing change time
        pref_since: Unix timestamp — client's last known preference change time
        sid: Client session ID — to filter out own changes

    Returns:
        {changed: bool, ts: float, assets: [...], session_id: str,
         prefs_changed: bool, prefs_ts: float}
    """
    user_id = session.get('user_id')
    if not user_id:
        return jsonify(error='Authentication required'), 401

    # ── Tier gate: cross-device sync requires paid tier ──
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    if not get_tier_limits(get_request_tier()).get('cross_device_sync'):
        return jsonify(changed=False, prefs_changed=False, tier_locked=True)

    since = request.args.get('since', 0, type=float)
    pref_since = request.args.get('pref_since', 0, type=float)
    client_sid = request.args.get('sid', '')

    # Check drawing changes
    ts, detail = get_last_change(user_id)
    drawing_changed = False
    if ts > since:
        if not (detail and client_sid and detail.get('session_id') == client_sid):
            drawing_changed = True
            logger.info("Poll: drawing change for user %s (ts=%.3f > since=%.3f)",
                        user_id, ts, since)

    # Check preference changes
    pref_ts, pref_detail = get_last_pref_change(user_id)
    prefs_changed = False
    if pref_ts > pref_since:
        if not (pref_detail and client_sid and pref_detail.get('session_id') == client_sid):
            prefs_changed = True
            logger.info("Poll: preference change for user %s (pref_ts=%.3f > pref_since=%.3f)",
                        user_id, pref_ts, pref_since)

    result = {
        'changed': drawing_changed,
        'ts': ts,
        'prefs_changed': prefs_changed,
        'prefs_ts': pref_ts,
    }

    if drawing_changed and detail:
        result['assets'] = detail.get('assets', [])
        result['session_id'] = detail.get('session_id', '')

    return jsonify(result)
