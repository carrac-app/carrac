"""The instance's own public address.

Every link or address the app *generates* (emails, push/ntfy clicks, share
URLs, webhook deep links, sitemap, copy-paste connection snippets) is rooted
here, so a self-hosted instance never points its users at somebody else's
domain. Read at call time so a configured env var always wins.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

_DEFAULT = 'http://localhost:5002'


def site_base_url() -> str:
    """Public origin, no trailing slash: PUBLIC_BASE_URL, else SITE_URL, else local dev."""
    return (os.getenv('PUBLIC_BASE_URL') or os.getenv('SITE_URL') or _DEFAULT).rstrip('/')


def site_host() -> str:
    """Host[:port] of the public origin, e.g. ``example.test`` (for typed-in examples)."""
    return urlsplit(site_base_url()).netloc or site_base_url()


SUPPORT_EMAIL_PLACEHOLDER = '[operator: set SUPPORT_EMAIL]'


def support_email() -> str:
    """Operator's support address (SUPPORT_EMAIL), or a visible placeholder.

    Never a default mailbox: a self-hosted instance must not send its users'
    privacy/refund requests to somebody else's inbox.
    """
    return (os.getenv('SUPPORT_EMAIL') or '').strip() or SUPPORT_EMAIL_PLACEHOLDER
