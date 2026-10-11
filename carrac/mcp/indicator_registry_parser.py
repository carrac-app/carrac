"""Parse static/js/indicator-registry.js into a Python-readable dict.

The JS file is the single source of truth (a project rule: new indicators are added there only). We do regex parsing
rather than maintain a duplicate Python registry.

Cached at module load. Re-parses if file mtime changes (cheap during dev).
"""

import logging
import os
import re
import threading
from pathlib import Path

logger = logging.getLogger('carrac.mcp.indicator_registry')

_REGISTRY_PATH = Path(__file__).resolve().parents[2] / 'static' / 'js' / 'indicator-registry.js'

_lock = threading.RLock()
_cache = {'mtime': 0, 'data': None, 'categories': None}


# Match each top-level entry: KEY: { ... } where KEY is UPPER_CASE
# We rely on balanced-brace heuristic — the JS is one entry per line.
_ENTRY_RE = re.compile(
    r"^\s{8}([A-Z][A-Z0-9_]*):\s*\{(.+?)\},?\s*$",
    re.MULTILINE | re.DOTALL,
)

_FIELD_PATTERNS = {
    'label':       re.compile(r"label:\s*'((?:\\'|[^'])*)'"),
    'shortLabel':  re.compile(r"shortLabel:\s*'((?:\\'|[^'])*)'"),
    'category':    re.compile(r"category:\s*'([^']+)'"),
    'type':        re.compile(r"type:\s*'([^']+)'"),
    'configKey':   re.compile(r"configKey:\s*'([^']+)'"),
    'paneId':      re.compile(r"paneId:\s*'([^']+)'"),
    'desc':        re.compile(r"desc:\s*'((?:\\'|[^'])*)'"),
}

_LIST_RE = {
    'calcParams': re.compile(r"calcParams:\s*\[([^\]]*)\]"),
    'states':     re.compile(r"states:\s*\[([^\]]*)\]"),
}


def _decode_js_string(s):
    # Handle a few common JS escapes — \u00d7, \u2212, \', \\.
    s = s.replace("\\'", "'").replace("\\\\", "\\")
    s = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)
    return s


def _parse_list(raw, as_int=False):
    items = [x.strip().strip("'\"") for x in raw.split(',') if x.strip()]
    if as_int:
        out = []
        for x in items:
            try:
                out.append(int(x))
            except ValueError:
                pass
        return out
    return items


def _parse_file(text):
    indicators = {}
    for match in _ENTRY_RE.finditer(text):
        key = match.group(1)
        body = match.group(2)
        entry = {'name': key}
        for field, pat in _FIELD_PATTERNS.items():
            m = pat.search(body)
            if m:
                entry[field] = _decode_js_string(m.group(1))
        for field, pat in _LIST_RE.items():
            m = pat.search(body)
            if m:
                entry[field] = _parse_list(m.group(1), as_int=(field == 'calcParams'))
        # Skip nested category objects accidentally matched (must have 'category' key).
        if 'category' not in entry:
            continue
        indicators[key] = entry
    return indicators


def _parse_categories(text):
    cats = []
    cat_block = re.search(r"var CATEGORIES\s*=\s*\[(.*?)\];", text, re.DOTALL)
    if not cat_block:
        return cats
    for m in re.finditer(r"\{\s*id:\s*'([^']+)',\s*label:\s*'([^']+)'", cat_block.group(1)):
        cats.append({'id': m.group(1), 'label': m.group(2)})
    return cats


def _refresh_if_stale():
    if not _REGISTRY_PATH.exists():
        logger.warning(f'Indicator registry not found at {_REGISTRY_PATH}')
        _cache['data'] = {}
        _cache['categories'] = []
        return
    try:
        mtime = os.path.getmtime(_REGISTRY_PATH)
    except OSError as e:
        logger.error(f'Indicator registry mtime failed: {e}')
        return
    with _lock:
        if _cache['data'] is not None and mtime == _cache['mtime']:
            return
        text = _REGISTRY_PATH.read_text(encoding='utf-8')
        _cache['data'] = _parse_file(text)
        _cache['categories'] = _parse_categories(text)
        _cache['mtime'] = mtime
        count = len(_cache['data'])
        if count == 0:
            logger.error(
                f'Registry parse returned 0 indicators — '
                f'path={_REGISTRY_PATH}, exists={_REGISTRY_PATH.exists()}'
            )
        else:
            logger.info(f'Indicator registry parsed: {count} indicators')


def get_all_indicators():
    _refresh_if_stale()
    return dict(_cache['data'] or {})


def get_categories():
    _refresh_if_stale()
    return list(_cache['categories'] or [])


def get_registry_diagnostics():
    """Return a dict with path/exists/mtime/count/stale for health checks and MCP warnings."""
    _refresh_if_stale()
    data = _cache.get('data') or {}
    cached_mtime = _cache.get('mtime') or 0
    try:
        current_mtime = os.path.getmtime(_REGISTRY_PATH) if _REGISTRY_PATH.exists() else None
    except OSError:
        current_mtime = None
    stale = (current_mtime is not None) and (current_mtime != cached_mtime)
    return {
        'path': str(_REGISTRY_PATH),
        'exists': _REGISTRY_PATH.exists(),
        'mtime': cached_mtime or None,
        'count': len(data),
        'stale': stale,
    }


# ── Lazy label-to-key index for fuzzy lookup ───────────────────────────────

_label_index_cache = {'built_for_mtime': None, 'index': None}
_label_index_lock = threading.Lock()


def _build_label_index(data):
    """Build {normalized_label: key, normalized_shortlabel: key} index."""
    idx = {}
    for key, entry in data.items():
        label = entry.get('label', '')
        short = entry.get('shortLabel', '')
        if label:
            idx[label.lower()] = key
        if short:
            short_norm = short.lower()
            if short_norm not in idx:
                idx[short_norm] = key
    return idx


def _get_label_index():
    _refresh_if_stale()
    data = _cache.get('data') or {}
    mtime = _cache.get('mtime')
    with _label_index_lock:
        if _label_index_cache['built_for_mtime'] != mtime or _label_index_cache['index'] is None:
            _label_index_cache['index'] = _build_label_index(data)
            _label_index_cache['built_for_mtime'] = mtime
        return _label_index_cache['index']


def _levenshtein(a, b):
    """Minimal inline Levenshtein for short strings (suggestions only)."""
    if len(a) < len(b):
        a, b = b, a
    row = list(range(len(b) + 1))
    for c in a:
        new_row = [row[0] + 1]
        for j, d in enumerate(b):
            new_row.append(min(new_row[-1] + 1, row[j + 1] + 1, row[j] + (c != d)))
        row = new_row
    return row[-1]


def _normalized_similarity(a, b):
    """1.0 = identical, 0.0 = completely different (normalised edit distance)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 1.0 - (_levenshtein(a, b) / max(len(a), len(b)))


# Below this normalized similarity, a Tier-3 substring "match" is rejected
# rather than returned with full confidence. Naive substring containment
# alone produces false-positive hard matches on near-miss/garbled input —
# e.g. "Boilinger Bandz" (a typo of "Bollinger Bands") coincidentally
# contains "oi", the shortLabel for the *unrelated* Open Interest indicator,
# and "xzqwmapqzx" coincidentally contains "ma" (MA's shortLabel). Without
# the floor both are returned as confident, wrong answers. The floor is set so real
# near-misses like "Bollinger Bands" (-> BOLL, ratio ~0.6) and "ATR"
# (-> ATR_PCT, ratio ~0.6) still match, while coincidental short-acronym
# collisions (ratio ~0.1-0.2) fall through to Tier 4 suggestions instead.
_FUZZY_SIMILARITY_FLOOR = 0.5


def get_indicator_fuzzy(name):
    """Look up an indicator by key or natural-language label with fallback tiers.

    Returns the entry dict on a match, or
    ``{"error": "...", "suggestions": [...]}`` on miss.

    Tiers:
      1. Direct key lookup (uppercase-normalised)
      2. Exact label/shortLabel match (case-insensitive)
      3. Substring match — input in label/shortLabel or vice-versa, gated by
         a normalized-edit-distance similarity floor (see
         _FUZZY_SIMILARITY_FLOOR) so a coincidental substring hit on a
         near-miss/garbled name doesn't return a confident wrong answer.
      4. Top-3 by Levenshtein distance across all labels — also the
         fallback when Tier 3 finds substring candidates but none clear
         the similarity floor.
    """
    _refresh_if_stale()
    data = _cache.get('data') or {}
    if not data:
        return {'error': f'Unknown indicator: {name}', 'suggestions': []}

    # Tier 1 — direct key
    key = name.upper().replace('-', '_').replace(' ', '_')
    if key in data:
        return data[key]

    # Tier 2 — exact label/shortLabel match
    label_idx = _get_label_index()
    name_lower = name.lower().strip()
    if name_lower in label_idx:
        return data[label_idx[name_lower]]

    # Tier 3 — substring match, gated by a similarity floor (see docstring
    # and _FUZZY_SIMILARITY_FLOOR comment above).
    substr_matches = []
    for entry_key, entry in data.items():
        label = entry.get('label', '').lower()
        short = entry.get('shortLabel', '').lower()
        if not (name_lower in label or name_lower in short
                or (label and label in name_lower) or (short and short in name_lower)):
            continue
        sim = max(
            _normalized_similarity(name_lower, label) if label else 0.0,
            _normalized_similarity(name_lower, short) if short else 0.0,
        )
        if sim >= _FUZZY_SIMILARITY_FLOOR:
            substr_matches.append((sim, entry))
    if substr_matches:
        # Highest similarity first (most specific / closest match).
        substr_matches.sort(key=lambda t: -t[0])
        return substr_matches[0][1]

    # Tier 4 — top-3 by edit distance across labels
    scored = []
    for entry_key, entry in data.items():
        label = entry.get('label', '')
        dist = min(
            _levenshtein(name_lower, label.lower()),
            _levenshtein(name_lower, entry_key.lower()),
        )
        scored.append((dist, entry_key, label))
    scored.sort(key=lambda x: x[0])
    suggestions = [
        {'key': k, 'label': lbl}
        for _, k, lbl in scored[:3]
    ]
    return {'error': f'Unknown indicator: {name}', 'suggestions': suggestions}
