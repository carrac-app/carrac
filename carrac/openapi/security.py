"""OpenAPI security scheme definitions."""


def get_security_schemes():
    """Return the 3 auth mechanisms supported by the API."""
    return {
        'api_key': {
            'type': 'apiKey',
            'in': 'header',
            'name': 'X-API-Key',
            'description': (
                'API key for programmatic access, available on every plan '
                '(rate limits and per-operation plan bars are described in the '
                'API introduction). '
                'Create keys in Settings > API & Developer.'
            ),
        },
        'session_cookie': {
            'type': 'apiKey',
            'in': 'cookie',
            'name': 'session',
            'description': 'Browser session cookie (web UI login).',
        },
        'widget_token': {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'cvx_w_*',
            'description': (
                'Widget token for mobile/embedded consumers. '
                'Limited to read-only summary data.'
            ),
        },
    }
