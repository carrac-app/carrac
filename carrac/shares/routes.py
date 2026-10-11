"""Public, no-auth share routes for Plays.

Anyone can open ``/s/<slug>`` with no Carrac account. The page is rendered
from the FROZEN snapshot captured at mint time, so it reveals only the fields
the creator opted to share and never changes once minted.

Writes (mint/update/revoke) live in the FastAPI plays router — the
``/api/plays/`` prefix is FastAPI-live via the strangler. The card image
(``/s/<slug>/card.png``) renders lazily on first request (Pillow, ~25ms),
persists to ``$DATA_DIR/shared/<slug>.png`` and is a plain file send after
that; render failures fall back to the static brand card so unfurls never
break.

These routes carry NO auth decorator by design. They fall through to Flask via
the strangler ``Mount("/", flask_asgi)`` because ``/s/`` and ``/api/s/`` are not
in any MIGRATED_* set.
"""

import logging
import os
import re
from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
)

from carrac.plays.shares import get_share_by_slug, set_image_path

logger = logging.getLogger('carrac.shares')

shares_bp = Blueprint('shares', __name__)

# Slugs are 8 chars over an unambiguous alphabet; accept 6–12 to be lenient on
# any future length tweak while still rejecting obvious junk before a DB hit.
_SLUG_RE = re.compile(r'^[a-z0-9]{6,12}$')


def _public_base():
    """Canonical public origin for absolute OG URLs.

    OG scrapers (X/Telegram/Discord/FB) do NOT resolve relative URLs, so
    every og:/twitter: URL must be absolute. Mirrors ``_public_base`` in the
    FastAPI plays router: explicit env first (prod sets ``SITE_URL``), the
    request's own origin as dev/test fallback.
    """
    base = os.getenv('PUBLIC_BASE_URL') or os.getenv('SITE_URL')
    if base:
        return base.rstrip('/')
    return request.url_root.rstrip('/')


def _resolve_share_or_abort(slug, *, increment_views=False):
    if not _SLUG_RE.match(slug or ''):
        abort(404)
    share = get_share_by_slug(slug, increment_views=increment_views)
    if not share:
        abort(404)
    if share.get('revoked_at'):
        abort(410)
    exp = share.get('expires_at')
    if exp:
        try:
            when = datetime.fromisoformat(str(exp).replace('Z', '+00:00'))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < datetime.now(timezone.utc):
                abort(404)
        except ValueError:
            pass
    return share


# kind -> public page template. The card renderer dispatches on kind itself
# (carrac/shares/card.py::render_card); resolver / 404 / 410 / rate limits /
# UTM are kind-agnostic.
_KIND_TEMPLATES = {
    'play': 'share_play.html',
    'trade': 'share_trade.html',
    'chart': 'share_chart.html',
}

# "Max-res hero" image: the RAW pre-composite chart image (see
# carrac.plays.shares._save_full_image) is saved next to the branded card as
# ``<slug>_full.<ext>`` in the SAME card_dir(). Presence + which extension
# exists IS the record of whether a share has a hero image at all — no new
# DB column or snapshot key (see _save_full_image's docstring for the
# reasoning). Checked in this order because the server-render path (today's
# only high-res mint path) always writes JPEG; PNG only shows up from an
# older/lower-res client capture.
_FULL_IMAGE_EXTS = (('jpg', 'image/jpeg'), ('png', 'image/png'))


def _find_full_image(slug):
    """Path + mimetype for a share's raw hero image, or ``(None, None)`` if
    it was never persisted (share predates this feature, or its render never
    produced full-fidelity bytes — e.g. the candles-only Pillow fallback)."""
    from carrac.shares.card import card_dir

    d = card_dir()
    for ext, mimetype in _FULL_IMAGE_EXTS:
        path = os.path.join(d, f'{slug}_full.{ext}')
        if os.path.isfile(path):
            return path, mimetype
    return None, None


def _interactive_share_enabled():
    """Interactive frozen-chart viewer kill switch — default ON. OFF must
    reproduce the pre-viewer page exactly (static hero / legacy candle
    render)."""
    return os.getenv('SHARE_INTERACTIVE_ENABLED', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


@shares_bp.route('/s/<slug>')
def share_page(slug):
    """Public SSR share page rendered from the frozen snapshot."""
    share = _resolve_share_or_abort(slug, increment_views=True)
    snap = share.get('snapshot') or {}
    base = _public_base()
    template = _KIND_TEMPLATES.get(share.get('kind') or 'play',
                                   'share_play.html')
    from carrac.shares.card import _fmt_price

    # Only chart shares can have a hero image — skip the filesystem check
    # entirely for play/trade pages.
    hero_url = None
    if share.get('kind') == 'chart':
        path, _mime = _find_full_image(slug)
        if path:
            hero_url = f'{base}/s/{slug}/chart.jpg'

    return render_template(
        template, share=share, snap=snap, slug=slug,
        base=base,
        page_url=f'{base}/s/{slug}',
        card_url=f'{base}/s/{slug}/card.png',
        hero_url=hero_url,
        fmt_price=_fmt_price,
        interactive=_interactive_share_enabled(),
    )


@shares_bp.route('/s/<slug>/card.png')
def share_card(slug):
    """The share's og:image — lazy Pillow render, file-cached, never breaks.

    No view increment here: scrapers + <img> fetches would double-count the
    page views.
    """
    share = _resolve_share_or_abort(slug)
    path = share.get('image_path')
    if not (path and os.path.isfile(path)):
        try:
            from carrac.shares.card import render_card_to_file
            path = render_card_to_file(share)
            set_image_path(share['id'], path)
        except Exception:
            logger.exception('share card render failed for %s — static fallback', slug)
            return redirect(f'{_public_base()}/static/og-dashboard.png', code=302)
    # Composited cards are now JPEG (<slug>.jpg), the Pillow sketch
    # stays PNG — serve the Content-Type that matches the actual file (the public
    # /card.png URL is stable regardless; unfurlers read the header, not the ext).
    mimetype = 'image/jpeg' if path.endswith('.jpg') else 'image/png'
    resp = send_file(os.path.abspath(path), mimetype=mimetype, conditional=True)
    # Snapshot is frozen; toggle changes NULL image_path and mint a fresh file.
    # A day of browser/CDN caching is safe — revocation hides the page itself.
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@shares_bp.route('/s/<slug>/chart.jpg')
def share_chart_hero(slug):
    """The raw, pre-composite hero image behind a chart share's page
    (the "max-res hero") — full resolution, no brand-frame
    letterboxing. Same no-auth/404/410 semantics as ``card.png``: unknown/
    malformed slug -> 404, revoked -> 410 (both via ``_resolve_share_or_abort``).

    Persisted only at MINT time (``_save_full_image``, never lazily like
    card.png) — a share with no hero bytes on disk (minted before this
    feature shipped, or whose render never produced full-fidelity bytes)
    404s here; the template only ever links to this route when the file is
    confirmed present, so that 404 is not reachable from the page itself.
    """
    _resolve_share_or_abort(slug)
    path, mimetype = _find_full_image(slug)
    if not path:
        abort(404)
    resp = send_file(os.path.abspath(path), mimetype=mimetype, conditional=True)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@shares_bp.route('/api/s/<slug>')
def share_json(slug):
    """Public JSON of the frozen snapshot (no auth)."""
    share = _resolve_share_or_abort(slug)
    return jsonify({'slug': slug, 'snapshot': share.get('snapshot') or {}})


# ── Branded error pages (blueprint-scoped: only /s/ + /api/s/ aborts) ──

def _share_error(status, title, message):
    if request.path.startswith('/api/'):
        return jsonify({'error': message}), status
    return render_template(
        'share_error.html', status=status, title=title, message=message,
    ), status


@shares_bp.errorhandler(404)
def share_not_found(_e):
    return _share_error(
        404, 'Share not found',
        'This share link does not exist or has expired.')


@shares_bp.errorhandler(410)
def share_gone(_e):
    return _share_error(
        410, 'Share revoked',
        'The owner has revoked this share link.')
