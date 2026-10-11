"""Top-level OpenAPI metadata: info, servers, tags."""

from . import API_VERSION
from carrac.site_url import site_base_url


def get_metadata():
    """Return the top-level OpenAPI spec dict (without paths/components)."""
    return {
        'openapi': '3.0.3',
        'info': {
            'title': 'Carrac API',
            'version': API_VERSION,
            'description': (
                'Market intelligence platform for crypto traders. '
                'Provides chart data, scanner scores, trade plays, alerts, '
                'journal, sentiment, quant analysis, and AI-powered insights.\n\n'
                '**Authentication**: send your API key in the `X-API-Key` header '
                '(create keys under Settings > API & Developer). A key acts as '
                'the user who created it. Browser sessions work too.\n\n'
                '**Plans**: every plan (free, member, premium) can use API keys. '
                'What a key may call depends on the plan: an operation that '
                'needs more than the free plan says so in its `x-min-tier` '
                'field (`member` or `premium`), and answers 403 below that '
                'plan. Some free-plan responses are also trimmed, for example '
                'the scanner returns a handful of rows.\n\n'
                '**Rate limits** (per key, sliding windows, all three apply):\n\n'
                '| Plan | Per minute | Per hour | Per day |\n'
                '|---|---|---|---|\n'
                '| free | 20 | 300 | 2,000 |\n'
                '| member | 60 | 1,500 | 15,000 |\n'
                '| premium | 120 | 5,000 | 50,000 |\n\n'
                'Admin keys are unlimited. A call over a limit answers 429 '
                'with a `Retry-After` header and a body naming the `limit`, '
                'the `window` and `retry_after` seconds. Some operations also '
                'carry their own, tighter per-route limit.\n\n'
                '**Compact Mode**: Add `?format=compact` to GET endpoints for '
                'token-efficient responses optimized for AI agents (~800 bytes vs ~15KB).'
            ),
            'contact': {
                'name': 'Carrac Support',
                'url': site_base_url(),
            },
        },
        'servers': [
            {'url': site_base_url(), 'description': 'This instance'},
        ],
        'tags': [
            {'name': 'Chart', 'description': 'OHLCV data, indicators, CPR levels'},
            {'name': 'Scanner', 'description': 'Market scanner scores, verdicts, transitions'},
            {'name': 'Plays', 'description': 'Trade thesis tracking with conditional logic'},
            {'name': 'Alerts', 'description': 'Price, indicator, and trendline alerts'},
            {'name': 'Journal', 'description': 'Trade journal with analytics and AI review'},
            {'name': 'Sentiment', 'description': 'Market sentiment, Fear & Greed, funding rates'},
            {'name': 'Reports', 'description': 'Morning analysis, weekly digest, afternoon update'},
            {'name': 'Drawings', 'description': 'Chart annotations, scenarios, trendlines'},
            {'name': 'Quant', 'description': 'Macro data, z-scores, regime detection, correlations'},
            {'name': 'AI', 'description': 'AI-powered chat, trade review, quick scoring'},
            {'name': 'Composite', 'description': 'Multi-source composite endpoints: asset detail, live positions, batch query'},
        ],
    }
