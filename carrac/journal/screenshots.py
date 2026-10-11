"""
Journal screenshot capture and storage.

Captures the chart viewport via the existing Playwright screenshot infrastructure
and stores it linked to a trade journal entry.
"""

import logging
import os
import re
import time

import requests

logger = logging.getLogger('carrac.journal.screenshots')

_SAFE_ASSET_RE = re.compile(r'^[A-Za-z0-9_]{1,20}$')
_SAFE_TF_RE = re.compile(r'^[A-Za-z0-9]{1,5}$')


def capture_journal_screenshot(db, journal_id: int, user_id: int,
                                asset: str, timeframe: str,
                                screenshot_type: str = 'context') -> dict:
    """Capture a chart screenshot and link it to a journal entry.

    Uses the internal screenshot API endpoint to generate the image,
    then stores it in the journal screenshots directory.

    Returns:
        dict with screenshot_id and file_path, or error.
    """
    if not _SAFE_ASSET_RE.match(asset) or not _SAFE_TF_RE.match(timeframe):
        return {'error': 'Invalid asset or timeframe'}

    data_dir = os.environ.get('DATA_DIR', 'data')
    screenshot_dir = os.path.join(data_dir, 'journal-screenshots', str(user_id), str(journal_id))
    os.makedirs(screenshot_dir, exist_ok=True)

    timestamp = int(time.time())
    filename = f"{screenshot_type}_{timestamp}.jpg"
    rel_path = os.path.join('journal-screenshots', str(user_id), str(journal_id), filename)
    full_path = os.path.join(data_dir, rel_path)

    # Use internal screenshot API
    api_key = os.getenv('SCREENSHOT_API_KEY', '')
    base_url = f"http://127.0.0.1:{int(os.getenv('PORT', '5000'))}"
    screenshot_url = f"{base_url}/api/screenshot/{asset}/{timeframe}"

    try:
        resp = requests.get(
            screenshot_url,
            params={'width': 1280, 'height': 720},
            headers={'X-Internal-Key': api_key},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error(f"Screenshot API returned {resp.status_code} for {asset}/{timeframe}")
            return {'error': f'Screenshot capture failed (status {resp.status_code})'}

        content_type = resp.headers.get('Content-Type', '')
        if 'image' not in content_type:
            return {'error': 'Screenshot API did not return an image'}

        with open(full_path, 'wb') as f:
            f.write(resp.content)

    except requests.RequestException as e:
        logger.error(f"Screenshot request failed: {e}")
        return {'error': 'Screenshot capture failed'}

    # Store in database (RAII pool conn avoids pinning a pool slot)
    from carrac.db_pool import get_conn as _pool_get_conn, returning_id, get_last_id
    with _pool_get_conn() as conn:
        cursor = conn.execute(
            f'''INSERT INTO journal_screenshots
               (journal_id, user_id, screenshot_type, file_path)
               VALUES (?, ?, ?, ?){returning_id()}''',
            (journal_id, user_id, screenshot_type, rel_path)
        )
        screenshot_id = get_last_id(cursor, conn)

    return {
        'screenshot_id': screenshot_id,
        'file_path': rel_path,
        'type': screenshot_type,
    }
