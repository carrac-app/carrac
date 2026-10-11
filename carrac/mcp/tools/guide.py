"""
MCP tool: get_guide — returns user guide content for a feature or topic.

Parses guide.html into sections by <h2> anchors at startup,
then returns matching section text on request.
"""

import logging
import os
import re
from pathlib import Path

from carrac.mcp.registry import mcp_tool

logger = logging.getLogger('carrac.mcp.tools.guide')

# Parsed guide sections: {section_id: {'title': str, 'text': str}}
# 'text' is the FULL stripped section — truncation happens per-request in
# _section_payload, not at load time. Truncating at load meant the
# full-text fallback search (step 4 below) only ever searched the first 3000
# characters, so a topic documented later in a long section was unreachable by
# any path — get_guide('backtest') returned MCP-setup boilerplate that stopped
# 1,406 characters short of the word "backtest", with no error and no notice.
_GUIDE_SECTIONS = {}
_GUIDE_LOADED = False

# Per-section return cap. Sized to clear the longest section in guide.html
# (~12.3k chars) so nothing truncates today; it is a runaway guard, not an
# editorial budget. When it does bite, say so — see _section_payload.
_MAX_SECTION_CHARS = 14000


def _strip_html(html: str) -> str:
    """Strip HTML tags and decode common entities."""
    text = re.sub(r'<[^>]+>', ' ', html)
    text = text.replace('&mdash;', '—').replace('&ndash;', '–')
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&rarr;', '→').replace('&larr;', '←')
    text = text.replace('&uarr;', '↑').replace('&darr;', '↓')
    text = text.replace('&ge;', '≥').replace('&le;', '≤')
    text = text.replace('&times;', '×').replace('&minus;', '−')
    text = text.replace('&#9432;', 'ℹ').replace('&#9733;', '★')
    text = text.replace('&quot;', '"').replace('&#10003;', '✓')
    # Curly quotes, ellipses and separators are all over guide.html
    # (17 &rsquo;, 19 &ldquo;/&rdquo;, 9 &middot;) and would otherwise reach the caller
    # as raw entity text, which reads as markup noise in an AI's context.
    text = text.replace('&lsquo;', '‘').replace('&rsquo;', '’')
    text = text.replace('&ldquo;', '“').replace('&rdquo;', '”')
    text = text.replace('&hellip;', '…').replace('&middot;', '·')
    text = text.replace('&nbsp;', ' ').replace('&trade;', '™')
    text = text.replace('&plusmn;', '±').replace('&#9888;', '⚠')
    text = text.replace('&#9998;', '✎').replace('&#8308;', '⁴')
    # Collapse whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _load_guide():
    """Parse guide.html and journal-guide.html into sections."""
    global _GUIDE_SECTIONS, _GUIDE_LOADED
    if _GUIDE_LOADED:
        return

    # Find templates directory
    # parents[0]=tools, parents[1]=mcp, parents[2]=carrac, parents[3]=project root (/app)
    base = Path(__file__).resolve().parents[3]
    guide_path = str(base / 'templates' / 'guide.html')
    journal_guide_path = str(base / 'templates' / 'journal-guide.html')

    # Parse main guide
    if os.path.exists(guide_path):
        try:
            with open(guide_path, 'r', encoding='utf-8') as f:
                html = f.read()
            # The page is a Jinja template; fill the instance address ourselves.
            from carrac.site_url import site_base_url, site_host
            html = html.replace('{{ site_url }}', site_base_url()).replace('{{ site_host }}', site_host())
            # Split by <h2 id="..."> tags
            parts = re.split(r'<h2\s+id="([^"]+)"[^>]*>', html)
            # parts[0] is before first h2, then alternating: id, content, id, content...
            for i in range(1, len(parts) - 1, 2):
                section_id = parts[i]
                section_html = parts[i + 1]
                # Extract until next h2 or end
                section_html = section_html.split('<h2')[0]
                # Get title from the content (first text before </h2> was already split)
                title_match = re.match(r'[^<]*', section_html)
                title = title_match.group(0).strip() if title_match else section_id
                text = _strip_html(section_html)
                if text:
                    _GUIDE_SECTIONS[section_id] = {'title': title, 'text': text}
        except Exception as e:
            logger.warning(f"Failed to parse guide.html: {e}")

    # Parse journal guide as a single section
    if os.path.exists(journal_guide_path):
        try:
            with open(journal_guide_path, 'r', encoding='utf-8') as f:
                html = f.read()
            from carrac.site_url import site_base_url, site_host
            html = html.replace('{{ site_url }}', site_base_url()).replace('{{ site_host }}', site_host())
            text = _strip_html(html)
            if text:
                _GUIDE_SECTIONS['journal-detail'] = {'title': 'Trade Journal (Detailed Guide)', 'text': text}
        except Exception as e:
            logger.warning(f"Failed to parse journal-guide.html: {e}")

    _GUIDE_LOADED = True
    if _GUIDE_SECTIONS:
        logger.info(f"Loaded {len(_GUIDE_SECTIONS)} guide sections")
    else:
        logger.error(
            f"get_guide: _GUIDE_SECTIONS is empty after load attempt — "
            f"guide_path={guide_path}, exists={os.path.exists(guide_path)}"
        )


# ── Topic aliases for fuzzy matching ──
_TOPIC_ALIASES = {
    'alert': 'alerts', 'notification': 'alerts', 'notify': 'alerts',
    'play': 'plays', 'hypothesis': 'plays', 'thesis': 'plays',
    'scan': 'scanner', 'score': 'scanner', 'verdict': 'scanner',
    'draw': 'drawing-tools', 'drawing': 'drawing-tools', 'trendline': 'drawing-tools',
    'indicator': 'indicators', 'ema': 'indicators', 'rsi': 'indicators', 'macd': 'indicators',
    'report': 'morning-report', 'morning': 'morning-report',
    'journal': 'journal', 'trade log': 'journal', 'trade journal': 'journal',
    'position': 'positions', 'tracker': 'positions',
    'quant': 'quant-engine', 'regime': 'quant-engine', 'macro': 'quant-engine',
    'chart': 'chart', 'timeframe': 'chart', 'candle': 'chart',
    'keyboard': 'shortcuts', 'shortcut': 'shortcuts', 'hotkey': 'shortcuts',
    'formula': 'custom-formulas', 'custom indicator': 'custom-formulas',
    'multi chart': 'multi-chart', 'multi-chart': 'multi-chart',
    'tier': 'tiers', 'premium': 'tiers', 'pricing': 'tiers',
    'api': 'api', 'mcp': 'api', 'rest': 'api',
    'mobile': 'mobile', 'pwa': 'mobile',
    'widget': 'widgets',
    'screener': 'screener', 'signal': 'screener',
    'sentiment': 'sentiment-bar', 'fear': 'sentiment-bar', 'greed': 'sentiment-bar',
    'layout': 'layout', 'view mode': 'layout', 'panel': 'layout',
    'getting started': 'getting-started', 'start': 'getting-started', 'setup': 'getting-started',
    'setting': 'settings', 'preference': 'settings',
    # 'backtest' must not alias to 'api': a section buried deep in the API
    # section falls past the per-section char cap and resolves to MCP-setup
    # boilerplate. The backtester has its own <h2 id="backtester"> section in
    # guide.html.
    'backtest': 'backtester', 'backtesting': 'backtester',
    'backtester': 'backtester', 'strategy': 'backtester',
    'simulate': 'backtester', 'simulation': 'backtester',
    'equity curve': 'backtester', 'win rate': 'backtester',
}


def _section_payload(section_id: str) -> dict:
    """Build the return payload for one section, truncating only if needed.

    When content IS dropped, say so. The previous behaviour cut at
    3000 characters mid-sentence with no signal, so a caller could not tell a
    complete answer from a silently amputated one.
    """
    sec = _GUIDE_SECTIONS[section_id]
    text = sec['text']
    payload = {'topic': section_id, 'title': sec['title']}
    if len(text) > _MAX_SECTION_CHARS:
        payload['content'] = text[:_MAX_SECTION_CHARS]
        payload['truncated'] = True
        payload['full_length'] = len(text)
        payload['note'] = (
            f'Section truncated at {_MAX_SECTION_CHARS} of {len(text)} characters. '
            f'The remainder is on the guide page at /guide#{section_id}.'
        )
    else:
        payload['content'] = text
        payload['truncated'] = False
    return payload


@mcp_tool(
    'get_guide',
    'Return Carrac\'s own usage documentation by section — the authoritative '
    'how-to reference for every feature in the app. '
    'Call with no topic (or omit topic) to list all available section IDs, titles, '
    'and known aliases. Call with a topic to get the full section text. '
    'Responses carry a "truncated" flag — false means you received the complete section. '
    'Topic matching is fuzzy: tries direct ID match → alias map → substring search → '
    'full-text search across all sections. '
    'Covers: getting-started, chart, indicators, scanner, plays, alerts, drawing-tools, '
    'backtester, morning-report, journal, positions, quant-engine, shortcuts, formulas, '
    'multi-chart, mobile, tiers, api, and more. '
    'Freshness: parsed from guide.html + journal-guide.html at server start — '
    'reflects the shipped version. '
    'Always call this tool first when the user asks "how do I…" or "what does X do" '
    'in Carrac before attempting to infer from other data sources.',
    {
        'type': 'object',
        'properties': {
            'topic': {
                'type': 'string',
                'description': (
                    'Topic to look up. Examples: "alerts", "plays", "scanner", '
                    '"drawing tools", "indicators", "journal", "backtest", '
                    '"backtester", "morning report", "quant engine", "shortcuts", '
                    '"chart", "getting started", "formulas", "positions", "tiers"'
                ),
            },
        },
        'required': [],
    },
    min_tier='free',
)
def get_guide(arguments: dict) -> dict:
    """Return user guide content for a topic."""
    _load_guide()

    topic = (arguments.get('topic') or '').lower().strip()
    if not topic:
        # Build reverse alias map: section_id -> [aliases...]
        reverse_aliases: dict = {}
        for alias, target in _TOPIC_ALIASES.items():
            reverse_aliases.setdefault(target, []).append(alias)
        sections_list = [
            {
                'id': sid,
                'title': sec.get('title') or sid,
                'aliases': reverse_aliases.get(sid, []),
            }
            for sid, sec in _GUIDE_SECTIONS.items()
        ]
        return {
            'sections': sections_list,
            'hint': 'Provide a topic id or alias to get guide content.',
        }

    # 1. Direct match
    if topic in _GUIDE_SECTIONS:
        return _section_payload(topic)

    # 2. Alias match
    alias = _TOPIC_ALIASES.get(topic)
    if alias and alias in _GUIDE_SECTIONS:
        return _section_payload(alias)

    # 3. Substring search in section IDs and titles
    for sid, sec in _GUIDE_SECTIONS.items():
        if topic in sid or topic in sec['title'].lower():
            return _section_payload(sid)

    # 4. Full-text search across all sections
    matches = []
    for sid, sec in _GUIDE_SECTIONS.items():
        if topic in sec['text'].lower():
            matches.append({'topic': sid, 'title': sec['title'], 'excerpt': sec['text'][:500]})
    if matches:
        return {'results': matches[:3], 'hint': f'Found {len(matches)} sections mentioning "{topic}".'}

    return {'error': f'No guide content found for "{topic}".', 'sections': list(_GUIDE_SECTIONS.keys())}
