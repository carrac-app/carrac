"""Minimal Postiz REST client — DRAFT-only, dry-run by default.

DESIGN CONSTRAINT: Postiz must NEVER auto-post. Every
tweet is hand-reviewed and approved by the operator before it goes out — Postiz
(self-hosted on the operator's own network) is a staging/approval queue
only. The X channel connect (OAuth in the Postiz UI) is performed by the
operator personally; this client never touches that flow.

HARD RULE — enforced in code, not just in this docstring: the only network
call this module ever makes to create a post (`_create_draft_post`) hardcodes
`"type": "draft"` via `_build_post_payload`'s guard, which RAISES if asked for
anything else. There is no "schedule" or "now" path anywhere in this module —
do not add one here; if trigger (B) or a later change needs scheduling, that
is a deliberate, separately-reviewed change, not something to slip in here.

Config:
  POSTIZ_URL           base URL of the self-hosted Postiz instance
                       (e.g. http://<postiz-host>:4007).
  POSTIZ_API_KEY       Postiz public-API key.
  POSTIZ_API_PATH      API path prefix under POSTIZ_URL (default
                       '/api/public/v1' — see the API-shape note below;
                       override if the self-hosted deploy differs).
  POSTIZ_X_INTEGRATION_ID  the X "integration" (Postiz's API term for what
                       the UI calls a "channel") id to post to — obtained via
                       GET {POSTIZ_URL}{POSTIZ_API_PATH}/integrations once the
                       operator has connected the X channel.
  SOCIAL_DRY_RUN       '0' to allow a real (non-dry-run) network call when
                       POSTIZ_URL + POSTIZ_API_KEY are both set. ANY other
                       value, INCLUDING UNSET, means dry-run — see
                       is_dry_run() below. This is deliberately
                       fail-safe-by-default: even a fully configured Postiz
                       connection stays dry-run until the operator explicitly
                       opts in.
  SOCIAL_LINK_URL      base URL used for the link-in-first-comment entry
                       (default: this instance's SITE_URL). The payload builder
                       appends `?utm_source=x&utm_medium=social&utm_campaign=
                       daily-<YYYY-MM-DD>` (date from the candidate's
                       created_at) — see _build_post_payload / _campaign_date.

API shape (WebFetch'd from https://docs.postiz.com/public-api/posts/create.md
and .../change-status.md):

    POST {base}/posts
    Authorization: <POSTIZ_API_KEY>
    {
      "type": "draft",              # "draft" | "schedule" | "now" — this
                                     # client only ever sends "draft"
      "date": "<ISO 8601>",
      "shortLink": false,
      "tags": [],
      "posts": [{
        "integration": {"id": "<integration-id>"},
        "value": [
          {"content": "<text, NO link>", "image": [{"id": ..., "path": ...}]},
          {"content": "More context on this read -> <link+utm>", "image": []}
        ],
        "settings": {"__type": "x", "who_can_reply_post": "everyone"}
      }]
    }

  Two requirements confirmed live against the real Postiz instance
  (not from the docs):

  * `settings.who_can_reply_post` is REQUIRED for X posts — Postiz 400s
    without it ("posts.0.settings.who_can_reply_post must be one of ...").
    Always send `"everyone"`.
  * link-in-first-comment: the main tweet (`value[0]`) must carry NO link
    (X suppresses link-post reach) — the instance link instead goes in a
    SECOND `value` entry (`value[1]`), which Postiz/X publishes as a first
    reply from the same account. The link is UTM-tagged with a
    `daily-<YYYY-MM-DD>` campaign built from the candidate's `created_at`
    date; the base URL is overridable via `SOCIAL_LINK_URL` (default
    the instance's own SITE_URL).

  "draft" is a real, documented post state (distinct from "schedule"/"now"):
  "Save as a draft. The post is created and stored against the integration
  but not scheduled or published." A later `PUT {base}/posts/{id}/status`
  with `{"status": "schedule"}` is how a human-approved draft would actually
  go live from inside the Postiz UI/queue — this client never calls that
  endpoint either; approval + any status flip is the operator's action in
  Postiz, not this pipeline's.

  Base path ambiguity (left for a follow-up to confirm against the live
  instance, NOT probed during development):
  the public-api overview page states the self-hosted base as
  `{POSTIZ_URL}/public/v1`, while create.md's own "Base URL" line says
  `{POSTIZ_URL}/api/public/v1`. POSTIZ_API_PATH defaults to the latter
  (create.md is the more specific, authoritative page for this exact call)
  but is overridable via env without a code change if that's wrong.

  Image upload shape is UNCONFIRMED: the docs index
  (https://docs.postiz.com/llms.txt) lists `/upload` and `/upload-from-url`
  as existing, multipart-capable endpoints, but the dedicated page
  (upload.md) 404'd and the only concrete shape found was the *CLI's*
  `postiz upload <file>` output, `{"id": "img-123", "path": "https://..."}`.
  `_upload_image` below assumes a `file` multipart field and that response
  shape — CONFIRM against a live Postiz instance before relying on it; until
  then a failed/unconfirmed upload degrades to a text-only draft rather than
  raising (see `_create_draft_post`). Left as a named follow-up.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger('carrac.social.postiz_client')

_DEFAULT_API_PATH = '/api/public/v1'


def is_dry_run() -> bool:
    """Whether stage_candidate() must NOT touch the network.

    True when POSTIZ_URL or POSTIZ_API_KEY is unset, OR when SOCIAL_DRY_RUN
    is anything other than the literal string '0' (including unset) — see
    the module docstring's Config section for the rationale.
    """
    if not os.getenv('POSTIZ_URL') or not os.getenv('POSTIZ_API_KEY'):
        return True
    return os.getenv('SOCIAL_DRY_RUN', '1') != '0'


def _campaign_date(candidate: dict[str, Any]) -> str:
    """YYYY-MM-DD for the link's utm_campaign tag, derived from the candidate's created_at.

    Falls back to today's UTC date if created_at is missing or unparseable —
    the campaign tag must never be blank.
    """
    created_at = candidate.get('created_at')
    if created_at:
        try:
            return datetime.fromisoformat(created_at).date().isoformat()
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc).date().isoformat()


def _build_link_comment(candidate: dict[str, Any]) -> str:
    """The second `value` entry's content — link-in-first-comment (see module docstring).

    Built from SOCIAL_LINK_URL (default: SITE_URL) + a
    daily-<YYYY-MM-DD> utm_campaign from the candidate's created_at date.
    Never call this for the main tweet content (value[0]) — X suppresses
    link-post reach, which is the whole reason the link lives in a reply.
    """
    from carrac.site_url import site_base_url
    base_url = os.getenv('SOCIAL_LINK_URL') or site_base_url() + '/'
    campaign_date = _campaign_date(candidate)
    link_url = f"{base_url}?utm_source=x&utm_medium=social&utm_campaign=daily-{campaign_date}"
    return f"More context on this read → {link_url}"


def _build_post_payload(
    candidate: dict[str, Any],
    integration_id: str,
    image_refs: Optional[list[dict[str, Any]]] = None,
    post_type: str = 'draft',
) -> dict[str, Any]:
    """Build the POST /posts body. DRAFT-ONLY GUARD: raises for any post_type != 'draft'.

    This is the single choke point that makes "auto-posting is operator-
    forbidden" a code fact rather than just a comment: nothing upstream in
    this module ever passes a different post_type, and anything that tried
    to would raise here instead of silently scheduling/publishing.

    Two live-verified requirements baked in here (see module
    docstring):
      * `settings.who_can_reply_post: "everyone"` — Postiz 400s on X posts
        without it.
      * link-in-first-comment: `value` carries exactly two entries — the
        main tweet (`value[0]`, NO link) and a second entry (`value[1]`)
        with the UTM-tagged instance link, which Postiz/X publishes as a
        first reply from the same account.
    """
    if post_type != 'draft':
        raise ValueError(
            f"postiz_client only ever creates draft posts — got post_type={post_type!r}. "
            "Auto-scheduling/publishing is forbidden by design."
        )
    return {
        'type': 'draft',
        'date': datetime.now(timezone.utc).isoformat(),
        'shortLink': False,
        'tags': [],
        'posts': [{
            'integration': {'id': integration_id},
            'value': [
                {
                    'content': candidate.get('caption', ''),
                    'image': image_refs or [],
                },
                {
                    'content': _build_link_comment(candidate),
                    'image': [],
                },
            ],
            'settings': {'__type': 'x', 'who_can_reply_post': 'everyone'},
        }],
    }


def _upload_image(base_url: str, api_path: str, api_key: str, image_path: str) -> dict[str, Any]:
    """Upload a local image to Postiz; returns an {'id', 'path'} ref for the post payload.

    ASSUMED SHAPE — see the module docstring's "Image upload shape is
    UNCONFIRMED" note. Isolated in its own function (like
    `_create_draft_post`'s network call) so it's the only thing that needs
    to change if the real shape differs.
    """
    with open(image_path, 'rb') as f:
        resp = requests.post(
            f"{base_url}{api_path}/upload",
            files={'file': f},
            headers={'Authorization': api_key},
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()


def _create_draft_post(candidate: dict[str, Any], integration_id: str) -> dict[str, Any]:
    """The ONLY function in this module that reaches the Postiz network to create a post.

    HARD RULE: always creates a DRAFT (see _build_post_payload's guard).
    Never add a schedule/publish call here or anywhere else in this module.
    """
    base_url = os.environ['POSTIZ_URL'].rstrip('/')
    api_path = os.getenv('POSTIZ_API_PATH', _DEFAULT_API_PATH)
    api_key = os.environ['POSTIZ_API_KEY']

    image_refs: list[dict[str, Any]] = []
    image_path = candidate.get('image_path')
    if image_path:
        try:
            image_refs = [_upload_image(base_url, api_path, api_key, image_path)]
        except Exception as e:
            # Unconfirmed-shape endpoint (see module docstring) — degrade to a
            # text-only draft rather than losing the caption entirely.
            logger.error("postiz: image upload failed (%s) — staging draft text-only", e)

    payload = _build_post_payload(candidate, integration_id, image_refs=image_refs, post_type='draft')

    resp = requests.post(
        f"{base_url}{api_path}/posts",
        json=payload,
        headers={'Authorization': api_key},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json() if resp.content else {}
    logger.info("postiz: draft created id=%s", body.get('id') if isinstance(body, dict) else '?')
    return {'status': 'draft', 'response': body}


def stage_candidate(candidate: dict[str, Any], dry_run: Optional[bool] = None) -> dict[str, Any]:
    """Stage a candidate dict into Postiz as a DRAFT — or log a dry-run, never publish.

    `dry_run`: None (default) defers to is_dry_run() (env-driven). True/False
    force the respective path — but note `False` still requires
    POSTIZ_X_INTEGRATION_ID to be configured; there is no fallback to
    "publish anyway".
    """
    effective_dry_run = is_dry_run() if dry_run is None else dry_run

    if effective_dry_run:
        logger.info(
            "social dry-run: WOULD stage a Postiz draft — asset=%s caption=%r image=%s",
            candidate.get('asset'),
            (candidate.get('caption') or '')[:80],
            candidate.get('image_path'),
        )
        return {'status': 'dry_run', 'detail': 'SOCIAL_DRY_RUN / no Postiz config — nothing staged, no network call made'}

    integration_id = os.getenv('POSTIZ_X_INTEGRATION_ID', '')
    if not integration_id:
        raise RuntimeError(
            'POSTIZ_X_INTEGRATION_ID is not set — connect the X channel in the Postiz UI '
            '(operator-only), look up its integration id via '
            'GET {POSTIZ_URL}/api/public/v1/integrations, and set that env var before '
            'staging a real (non-dry-run) draft.'
        )

    return _create_draft_post(candidate, integration_id)
