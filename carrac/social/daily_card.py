"""Trigger (A): the scheduled daily social card.

Assembles ONE candidate/day from real data already produced by the morning
engine — no new detection logic here:

  * caption = the FIRST entry of the latest GENERAL report's `x_candidates`
    metadata. These are already per-candidate grounding-gated (see morning_engine/engine.py's _gate_filter_x_candidates)
    so no additional scan runs here. If there is no general report yet, or it
    has no x_candidates, NO candidate is created (logged) — bare/ungrounded
    posts are commodity noise and must be rejected, not synthesized.
  * image  = a chart screenshot via the existing headless-render service,
    reached over the same same-process HTTP loopback the frontend already
    uses (mirrors carrac/reports/screenshots.py's screenshot_proxy /
    carrac/journal/screenshots.py's capture_journal_screenshot pattern):
    a plain `requests.get` to `/api/screenshot/<asset>/<timeframe>` with
    `X-Internal-Key` from env SCREENSHOT_API_KEY, no `indicators` param so the
    server applies its own default set. Default asset/TF is BTC / 1D (the
    canonical daily-TF spelling per carrac/charts/routes.py's _TF_CANON).

The resulting candidate dict is persisted as JSON under data/social/ with
status 'pending' — this module does NOT talk to Postiz; see postiz_client.py
for the (dry-run-by-default) staging step, wired together by run()/main()
below as the manual CLI entry point:

    python -m carrac.social.daily_card [--dry-run] [--asset BTC] [--timeframe 1D]

No cron/scheduler wiring in this pass (deploy decision, deferred) — the
operator runs this by hand until that's decided.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger('carrac.social.daily_card')

DEFAULT_ASSET = 'BTC'
# Canonical daily-TF spelling (uppercase D) — see carrac/charts/routes.py
# _TF_CANON ('1d' -> '1D'); intraday TFs are lowercase, daily+ are upper.
DEFAULT_TIMEFRAME = '1D'

# Same validation the screenshot endpoints already apply (carrac/reports/screenshots.py).
_SAFE_ASSET_RE = re.compile(r'^[A-Za-z0-9_]{1,20}$')
_SAFE_TF_RE = re.compile(r'^[A-Za-z0-9]{1,5}$')


def _social_dir() -> str:
    """data/social/ (DATA_DIR-relative, same convention as carrac/journal/screenshots.py). Created on demand."""
    data_dir = os.environ.get('DATA_DIR', 'data')
    path = os.path.join(data_dir, 'social')
    os.makedirs(path, exist_ok=True)
    return path


def fetch_first_x_candidate() -> Optional[str]:
    """First `x_candidates` string from the latest GENERAL report's metadata.

    Reads the underlying store directly (carrac.content_db) the same
    way carrac/fastapi_app/routers/reports.py's GET /api/report/morning/latest
    does for its non-operator path — report_type='general',
    json.loads(report['metadata']), meta.get('x_candidates') — rather than
    calling that HTTP endpoint, since this runs in the same process/container
    as the engine and needs none of that route's tier/session/personal-section
    machinery.

    Returns None (logged) when there's no general report yet, or it has no
    (non-empty) x_candidates list, or the first entry isn't a usable string.
    Callers must treat None as "nothing to post today" — never synthesize a
    caption.
    """
    from carrac.content_db import get_content_db

    db = get_content_db()
    report = db.get_latest_report(report_type='general')
    if not report:
        logger.warning("daily_card: no general report found yet — skipping caption")
        return None

    meta = None
    if report.get('metadata'):
        try:
            meta = json.loads(report['metadata'])
        except (json.JSONDecodeError, TypeError):
            meta = None

    candidates = meta.get('x_candidates') if isinstance(meta, dict) else None
    if not isinstance(candidates, list) or not candidates:
        logger.warning(
            "daily_card: latest general report (id=%s) has no x_candidates — skipping caption",
            report.get('id'),
        )
        return None

    # A blank/invalid FIRST entry still means "skip today" — that guard predates
    # this change and is deliberate: a malformed candidate list is a signal that
    # something upstream is wrong, not an invitation to pick a different tweet.
    first = candidates[0]
    if not isinstance(first, str) or not first.strip():
        logger.warning("daily_card: first x_candidate is empty/invalid — skipping caption")
        return None

    # Candidates generated before the licensing gate can narrate
    # Deribit data, and this function's output is published verbatim. Filter
    # here as well as at generation — 27 stored candidates predate the gate and
    # will sit in the DB until they age out. Unlike the blank case above, moving
    # past a barred candidate to a clean one IS right: the list is well-formed,
    # one entry simply cannot be published.
    from carrac.reports.sanitize import public_x_candidates
    publishable = public_x_candidates(candidates)
    if not publishable:
        logger.warning(
            "daily_card: all %d candidates for report id=%s were withheld "
            "(licensing) — nothing to post today", len(candidates), report.get('id'),
        )
        return None

    return publishable[0]


def capture_chart_screenshot(
    asset: str = DEFAULT_ASSET,
    timeframe: str = DEFAULT_TIMEFRAME,
    width: int = 1280,
    height: int = 720,
) -> Optional[str]:
    """Chart image via the same-process screenshot service loopback.

    Mirrors carrac/reports/screenshots.py's screenshot_proxy /
    carrac/journal/screenshots.py's capture_journal_screenshot: a plain
    `requests.get` against the internal keyed endpoint
    (`http://127.0.0.1:<PORT>/api/screenshot/<asset>/<timeframe>`),
    `X-Internal-Key` from SCREENSHOT_API_KEY. No `indicators` query param is sent
    so the server falls back to its own default set
    (_DEFAULT_SCREENSHOT_INDICATORS).

    Saves the JPEG bytes under data/social/ and returns the file path, or
    None (logged) on any validation, config, network, or write failure.
    """
    if not _SAFE_ASSET_RE.match(asset) or not _SAFE_TF_RE.match(timeframe):
        logger.error("daily_card: invalid asset/timeframe %r/%r", asset, timeframe)
        return None

    api_key = os.getenv('SCREENSHOT_API_KEY', '')
    if not api_key:
        logger.error("daily_card: SCREENSHOT_API_KEY not configured — cannot capture chart")
        return None

    base_url = f"http://127.0.0.1:{int(os.getenv('PORT', '5000'))}"
    try:
        resp = requests.get(
            f"{base_url}/api/screenshot/{asset}/{timeframe}",
            params={'width': width, 'height': height},
            headers={'X-Internal-Key': api_key},
            timeout=60,
        )
    except requests.RequestException as e:
        logger.error("daily_card: screenshot request failed for %s/%s: %s", asset, timeframe, e)
        return None

    if resp.status_code != 200 or 'image' not in resp.headers.get('Content-Type', ''):
        logger.error(
            "daily_card: screenshot service returned status=%s for %s/%s",
            resp.status_code, asset, timeframe,
        )
        return None

    out_dir = _social_dir()
    filename = f"{asset}_{timeframe}_{int(time.time())}.jpg"
    path = os.path.join(out_dir, filename)
    try:
        with open(path, 'wb') as f:
            f.write(resp.content)
    except OSError as e:
        logger.error("daily_card: could not write screenshot to %s: %s", path, e)
        return None

    return path


def assemble_daily_candidate(
    asset: str = DEFAULT_ASSET,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> Optional[dict[str, Any]]:
    """Assemble ONE daily card candidate, or None if there's nothing groundable to post.

    Returns None WITHOUT persisting anything (and without attempting a
    screenshot) when there's no usable caption — an image with no grounded
    caption is not a valid candidate under the tweet-format rule.
    """
    caption = fetch_first_x_candidate()
    if not caption:
        return None

    image_path = capture_chart_screenshot(asset=asset, timeframe=timeframe)

    return {
        'caption': caption,
        'image_path': image_path,
        'asset': asset,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'status': 'pending',
    }


def persist_candidate(candidate: dict[str, Any]) -> str:
    """Persist a candidate dict as JSON under data/social/; returns its file path."""
    out_dir = _social_dir()
    stamp = re.sub(r'[^0-9]', '', candidate.get('created_at', '')) or str(int(time.time()))
    path = os.path.join(out_dir, f"candidate_{stamp}.json")
    with open(path, 'w') as f:
        json.dump(candidate, f, indent=2)
    return path


def run(dry_run: bool = False, asset: str = DEFAULT_ASSET, timeframe: str = DEFAULT_TIMEFRAME) -> int:
    """Build today's candidate and stage it via postiz_client (the CLI entry point body).

    `dry_run=True` forces dry-run regardless of env; `dry_run=False` still
    defers to postiz_client's own env-driven default (see its docstring) —
    passing --dry-run on the CLI is a belt-and-suspenders override, not the
    only safety net.
    """
    from carrac.social.postiz_client import stage_candidate

    candidate = assemble_daily_candidate(asset=asset, timeframe=timeframe)
    if candidate is None:
        print("No x_candidates available in the latest general report — nothing to stage today.")
        return 1

    candidate_path = persist_candidate(candidate)
    print(f"Candidate assembled: {candidate_path}")
    print(f"  asset={candidate['asset']} image={candidate['image_path']}")
    print(f"  caption: {candidate['caption'][:120]}")

    result = stage_candidate(candidate, dry_run=dry_run or None)
    candidate['status'] = result.get('status', candidate['status'])
    with open(candidate_path, 'w') as f:
        json.dump(candidate, f, indent=2)

    print(f"Postiz stage result: {result}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Scheduled daily card: assemble + stage today's founder-posted data card. "
            "NEVER auto-posts — Postiz DRAFT/pending-review only; every tweet is hand-"
            "reviewed and approved by the operator before it goes out."
        )
    )
    p.add_argument(
        '--dry-run', action='store_true',
        help='Force dry-run even if POSTIZ_URL/POSTIZ_API_KEY are configured '
             '(default behavior is ALSO dry-run whenever they are unset or SOCIAL_DRY_RUN != "0").',
    )
    p.add_argument('--asset', default=DEFAULT_ASSET)
    p.add_argument('--timeframe', default=DEFAULT_TIMEFRAME)
    args = p.parse_args()
    return run(dry_run=args.dry_run, asset=args.asset, timeframe=args.timeframe)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    sys.exit(main() or 0)
