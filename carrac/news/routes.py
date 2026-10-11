"""
News routes: RSS feed aggregation for the ticker bar.
"""

import hashlib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from defusedxml.ElementTree import fromstring as safe_xml_parse
from flask import Blueprint, jsonify, request

from carrac.auth.decorators import auth_or_key_required, login_required

logger = logging.getLogger('carrac.news')

news_bp = Blueprint('news', __name__)

# Hardcoded feed registry — no user-supplied URLs (SSRF-safe)
_FEEDS = {
    'cnbc_top': {
        'url': 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114',
        'name': 'CNBC',
    },
    'dlnews': {
        'url': 'https://www.dlnews.com/arc/outboundfeeds/rss/',
        'name': 'DL News',
    },
    'coindesk': {
        'url': 'https://www.coindesk.com/arc/outboundfeeds/rss/',
        'name': 'CoinDesk',
    },
    'cointelegraph': {
        'url': 'https://cointelegraph.com/rss',
        'name': 'CoinTelegraph',
    },
    'theblock': {
        'url': 'https://www.theblock.co/rss.xml',
        'name': 'The Block',
    },
    'decrypt': {
        'url': 'https://decrypt.co/feed',
        'name': 'Decrypt',
    },
    'marketwatch': {
        'url': 'https://feeds.content.dowjones.io/public/rss/mw_topstories',
        'name': 'MarketWatch',
    },
    'seekingalpha': {
        'url': 'https://seekingalpha.com/market_currents.xml',
        'name': 'Seeking Alpha',
    },
    'fed': {
        'url': 'https://www.federalreserve.gov/feeds/press_all.xml',
        'name': 'Federal Reserve',
    },
    'fxstreet': {
        'url': 'https://www.fxstreet.com/rss/news',
        'name': 'FXStreet',
    },
}

_VALID_FEED_IDS = frozenset(_FEEDS.keys())

# Response cache: keyed by frozenset of feed IDs
_news_cache = {}
_news_lock = threading.Lock()
_NEWS_TTL = 300  # 5 minutes

_MAX_ITEMS = 30
_FETCH_TIMEOUT = 10
_MAX_CUSTOM_FEEDS = 5
_MAX_CUSTOM_URL_LEN = 500
_CUSTOM_FEED_TIMEOUT = 8
_MAX_RESPONSE_SIZE = 1024 * 1024  # 1MB


def _validate_custom_feed_url(url):
    """Validate a user-supplied RSS feed URL for SSRF safety.

    Returns (ok, error_or_None, pinned_ip_or_None) so callers can
    dial the pinned IP directly instead of re-resolving the hostname, which
    would be vulnerable to DNS rebinding attacks.

    Previous signature was (ok, error_or_None). Callers updated to handle
    the third element; internal helper _fetch_pinned keeps backward compat.
    """
    if not url or not isinstance(url, str):
        return False, 'URL is required', None
    if len(url) > _MAX_CUSTOM_URL_LEN:
        return False, 'URL too long', None

    from security import validate_outbound_url
    ok, info = validate_outbound_url(url, allowed_schemes=('https',))
    if ok:
        # info is the pinned IP on success
        return True, None, info
    return False, info, None


def _fetch_feed_pinned(url, pinned_ip):
    """Fetch an RSS URL dialled to a pre-resolved (pinned) IP.

    Avoids a second DNS resolution that could be flipped by a
    DNS rebind attack. We connect to the pinned IP directly and pass the
    original hostname in the Host header so TLS SNI and certificate
    verification still work correctly.

    Falls back to a direct requests.get(url) if pinned_ip is None (should
    not happen in normal flow but provides a safe fallback for unit tests
    that do not mock socket.getaddrinfo).
    """
    if not pinned_ip:
        import requests as _req
        return _req.get(
            url,
            timeout=_CUSTOM_FEED_TIMEOUT,
            headers={'User-Agent': 'Carrac/1.0 RSS Reader'},
            stream=True,
        )

    from urllib.parse import urlparse, urlunparse
    import requests as _req
    from requests.adapters import HTTPAdapter

    parsed = urlparse(url)
    original_host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)

    # Build a URL that replaces the hostname with the pinned IP so requests
    # dials the correct address without a second DNS lookup.
    # We keep scheme/path/query/fragment intact.
    netloc_pinned = f'[{pinned_ip}]:{port}' if ':' in pinned_ip else f'{pinned_ip}:{port}'
    pinned_url = urlunparse((
        parsed.scheme,
        netloc_pinned,
        parsed.path or '/',
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))

    # Pass the real hostname in Host + SNI so TLS certificate validation
    # uses the domain name, not the IP.
    headers = {
        'User-Agent': 'Carrac/1.0 RSS Reader',
        'Host': original_host,
    }

    # Mount a custom HTTPAdapter that overrides the SNI hostname so the
    # TLS handshake validates the certificate against the original domain.
    class _PinnedSNIAdapter(HTTPAdapter):
        # server_hostname/assert_hostname are urllib3 POOL kwargs — NOT
        # HTTPAdapter.send() kwargs; routing them through send() raises
        # TypeError before any socket opens (same fix as
        # carrac/alerts/webhook_delivery.py::_post_pinned).
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            pool_kwargs['server_hostname'] = original_host
            pool_kwargs['assert_hostname'] = original_host
            return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

    session = _req.Session()
    session.mount('https://', _PinnedSNIAdapter())
    try:
        return session.get(
            pinned_url,
            timeout=_CUSTOM_FEED_TIMEOUT,
            headers=headers,
            stream=True,
            verify=True,
            # Never follow redirects on a user-supplied feed URL: a 3xx would
            # trigger a fresh, UNVALIDATED DNS resolution to the Location host,
            # bypassing the private-IP/rebind pinning that only ran against the
            # original URL. Refuse the redirect outright (SSRF hardening).
            allow_redirects=False,
        )
    finally:
        session.close()


def _strip_html(text):
    """Remove HTML tags and control characters from text."""
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    return text.strip()


def _parse_rss_date(date_str):
    """Parse RSS (RFC 822) or Atom (ISO 8601) date strings.

    All returned datetimes are normalized to UTC so that .isoformat()
    produces comparable strings for chronological sorting.
    """
    if not date_str:
        return None

    dt = None
    # Try RFC 822 first (RSS 2.0)
    try:
        dt = parsedate_to_datetime(date_str)
    except Exception:
        pass

    # Try ISO 8601 (Atom)
    if dt is None:
        for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S'):
            try:
                dt = datetime.strptime(date_str, fmt)
                break
            except ValueError:
                continue

    if dt is None:
        return None

    # Normalize to UTC — naive datetimes assumed UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fetch_single_feed(feed_id, feed_info):
    """Fetch and parse a single RSS/Atom feed. Returns (items, status, feed_title).

    Uses pinned IP from _validate_custom_feed_url when available to
    prevent DNS rebinding between validation and fetch time.
    """
    items = []
    feed_title = None
    try:
        pinned_ip = feed_info.get('_pinned_ip')
        if pinned_ip:
            resp = _fetch_feed_pinned(feed_info['url'], pinned_ip)
        else:
            resp = requests.get(
                feed_info['url'],
                timeout=_FETCH_TIMEOUT,
                headers={'User-Agent': 'Carrac/1.0 RSS Reader'},
                stream=True,
            )
        if resp.status_code != 200:
            logger.debug(f"Feed {feed_id} returned {resp.status_code}")
            return items, 'error', feed_title

        # Enforce response size limit to prevent memory exhaustion
        content_len = resp.headers.get('Content-Length')
        if content_len and int(content_len) > _MAX_RESPONSE_SIZE:
            logger.warning(f"Feed {feed_id} too large ({content_len} bytes)")
            resp.close()
            return items, 'error', feed_title
        body = resp.content
        if len(body) > _MAX_RESPONSE_SIZE:
            logger.warning(f"Feed {feed_id} too large ({len(body)} bytes)")
            return items, 'error', feed_title

        root = safe_xml_parse(body)
        tag = root.tag.lower()

        # Strip namespace for easier matching
        def _strip_ns(t):
            return t.split('}', 1)[-1] if '}' in t else t

        # RSS 2.0: <rss><channel><item>
        if _strip_ns(tag) == 'rss':
            channel = root.find('.//{http://purl.org/rss/1.0/}channel')
            if channel is None:
                for child in root:
                    if _strip_ns(child.tag) == 'channel':
                        channel = child
                        break
            if channel is not None:
                # Extract feed title from <channel><title>
                for sub in channel:
                    if _strip_ns(sub.tag) == 'title' and sub.text:
                        feed_title = _strip_html(sub.text)
                        break
                for item_el in channel:
                    if _strip_ns(item_el.tag) != 'item':
                        continue
                    item = _parse_rss_item(item_el, feed_id, feed_info['name'])
                    if item:
                        items.append(item)

        # Atom: <feed><entry>
        elif _strip_ns(tag) == 'feed':
            ns = ''
            if '}' in root.tag:
                ns = root.tag.split('}')[0] + '}'
            # Extract feed title from <feed><title>
            title_el = root.find(f'{ns}title')
            if title_el is not None and title_el.text:
                feed_title = _strip_html(title_el.text)
            for entry in root.findall(f'{ns}entry'):
                item = _parse_atom_entry(entry, ns, feed_id, feed_info['name'])
                if item:
                    items.append(item)

        # RDF/RSS 1.0 fallback
        else:
            for child in root:
                if _strip_ns(child.tag) == 'item':
                    item = _parse_rss_item(child, feed_id, feed_info['name'])
                    if item:
                        items.append(item)

        return items, 'ok', feed_title

    except Exception as e:
        logger.debug(f"Feed {feed_id} error: {e}")
        return items, 'error', feed_title


def _find_text(el, names):
    """Find text content from an element by trying multiple tag names."""
    for child in el:
        tag = child.tag.split('}', 1)[-1] if '}' in child.tag else child.tag
        if tag in names:
            return child.text
    return None


def _parse_rss_item(item_el, feed_id, source_name):
    """Parse a single RSS <item> element."""
    title = _find_text(item_el, ('title',))
    if not title:
        return None
    title = _strip_html(title)

    link = _find_text(item_el, ('link',))
    summary = _find_text(item_el, ('description',))
    pub_date = _find_text(item_el, ('pubDate', 'date', 'published'))

    summary = _strip_html(summary or '')
    if len(summary) > 500:
        summary = summary[:497] + '...'

    published = _parse_rss_date(pub_date)

    # Validate link
    if link and not link.startswith(('http://', 'https://')):
        link = None

    item_id = hashlib.md5((feed_id + title).encode(), usedforsecurity=False).hexdigest()[:12]

    return {
        'id': item_id,
        'title': title,
        'link': link,
        'source': source_name,
        'source_id': feed_id,
        'summary': summary,
        'published': published.isoformat() if published else None,
    }


def _parse_atom_entry(entry, ns, feed_id, source_name):
    """Parse a single Atom <entry> element."""
    title_el = entry.find(f'{ns}title')
    title = title_el.text if title_el is not None else None
    if not title:
        return None
    title = _strip_html(title)

    link = None
    for link_el in entry.findall(f'{ns}link'):
        href = link_el.get('href', '')
        rel = link_el.get('rel', 'alternate')
        if rel == 'alternate' and href:
            link = href
            break
    if not link:
        for link_el in entry.findall(f'{ns}link'):
            href = link_el.get('href', '')
            if href:
                link = href
                break

    summary_el = entry.find(f'{ns}summary') or entry.find(f'{ns}content')
    summary = summary_el.text if summary_el is not None else ''
    summary = _strip_html(summary or '')
    if len(summary) > 500:
        summary = summary[:497] + '...'

    pub_el = entry.find(f'{ns}published') or entry.find(f'{ns}updated')
    pub_date = pub_el.text if pub_el is not None else None
    published = _parse_rss_date(pub_date)

    if link and not link.startswith(('http://', 'https://')):
        link = None

    item_id = hashlib.md5((feed_id + title).encode(), usedforsecurity=False).hexdigest()[:12]

    return {
        'id': item_id,
        'title': title,
        'link': link,
        'source': source_name,
        'source_id': feed_id,
        'summary': summary,
        'published': published.isoformat() if published else None,
    }


@news_bp.route('/api/market-news')
@auth_or_key_required
def api_market_news():
    """Get aggregated news headlines from RSS feeds."""
    # Parse requested feed IDs
    feeds_param = request.args.get('feeds', '')
    if feeds_param:
        requested = frozenset(f.strip() for f in feeds_param.split(',') if f.strip() in _VALID_FEED_IDS)
    else:
        requested = frozenset(['coindesk', 'cointelegraph', 'decrypt'])

    # Parse custom feed URLs (premium-only)
    custom_urls = []
    custom_param = request.args.get('custom', '')
    if custom_param:
        from carrac.tiers import get_request_tier
        tier = get_request_tier()
        if tier in ('member', 'premium', 'admin', 'tester'):
            raw_urls = [u.strip() for u in custom_param.split(',') if u.strip()]
            for u in raw_urls[:_MAX_CUSTOM_FEEDS]:
                ok, _, _pinned = _validate_custom_feed_url(u)
                if ok:
                    custom_urls.append((u, _pinned))

    if not requested and not custom_urls:
        return jsonify({'status': 'ok', 'items': [], 'feeds': {}, 'timestamp': datetime.now().isoformat()})

    # Check cache (include custom URLs in key)
    cache_key = (requested, frozenset(u for u, _ in custom_urls) if custom_urls else frozenset())
    with _news_lock:
        cached = _news_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _NEWS_TTL:
            return jsonify(cached[1])

    # Fetch feeds in parallel
    all_items = []
    feed_statuses = {}

    feeds_to_fetch = {fid: _FEEDS[fid] for fid in requested}

    # Add custom feeds to the fetch map (include pinned IP)
    for curl, cpinned in custom_urls:
        fid = 'custom_' + hashlib.md5(curl.encode(), usedforsecurity=False).hexdigest()[:8]
        domain = urlparse(curl).hostname or 'Custom'
        # Strip common prefixes
        if domain.startswith('www.'):
            domain = domain[4:]
        feeds_to_fetch[fid] = {'url': curl, 'name': domain, '_pinned_ip': cpinned}

    with ThreadPoolExecutor(max_workers=min(len(feeds_to_fetch), 8)) as executor:
        futures = {
            fid: executor.submit(_fetch_single_feed, fid, finfo)
            for fid, finfo in feeds_to_fetch.items()
        }
        for fid, future in futures.items():
            try:
                items, status, _ = future.result(timeout=15)
                all_items.extend(items)
                feed_statuses[fid] = status
            except Exception as e:
                logger.warning(f"Feed {fid} timed out: {e}")
                feed_statuses[fid] = 'error'

    # Sort by published date (newest first), None dates last
    all_items.sort(key=lambda x: x.get('published') or '0000', reverse=True)

    # Limit to max items
    all_items = all_items[:_MAX_ITEMS]

    result = {
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'items': all_items,
        'feeds': feed_statuses,
    }

    # Cache
    with _news_lock:
        _news_cache[cache_key] = (time.time(), result)

    return jsonify(result)


@news_bp.route('/api/market-news/test', methods=['POST'])
@login_required
def api_test_feed():
    """Test a user-supplied RSS feed URL. Premium-only."""
    from carrac.tiers import get_request_tier
    tier = get_request_tier()
    if tier not in ('member', 'premium', 'admin', 'tester'):
        return jsonify({'status': 'error', 'error': 'Premium feature'}), 403

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()

    ok, err, pinned_ip = _validate_custom_feed_url(url)
    if not ok:
        return jsonify({'status': 'error', 'error': err})

    # Fetch and parse — pass pinned IP to avoid second DNS resolution
    fid = 'test_' + hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:8]
    domain = urlparse(url).hostname or 'Custom'
    if domain.startswith('www.'):
        domain = domain[4:]

    items, status, feed_title_xml = _fetch_single_feed(fid, {'url': url, 'name': domain, '_pinned_ip': pinned_ip})
    if status != 'ok' or not items:
        return jsonify({'status': 'error', 'error': 'Could not parse RSS/Atom feed from this URL'})

    feed_title = feed_title_xml or domain

    return jsonify({
        'status': 'ok',
        'title': feed_title[:50],
        'items': items[:3],
        'item_count': len(items),
    })
