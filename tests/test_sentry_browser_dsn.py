"""Browser error reporting is opt-in and never ships a baked-in DSN or an email."""
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
DSN = 'https://abc123pubkey@errors.example.test/7'
SERVER_DSN = 'http://srvkey@bugsink.internal:8000/7'


def _envelope(dsn=DSN):
    return ('{"event_id":"1","dsn":"%s"}\n{"type":"event"}\n{}' % dsn).encode() if dsn \
        else b'{"event_id":"1"}\n{"type":"event"}\n{}'


def _page(auth_client):
    return auth_client.get('/').data.decode()


def test_no_sdk_and_no_dsn_when_unset(auth_client, monkeypatch):
    monkeypatch.delenv('SENTRY_BROWSER_DSN', raising=False)
    html = _page(auth_client)
    assert 'Sentry.init({' not in html
    assert 'vendor/sentry.bundle' not in html
    assert 'carrac.cc/1' not in html


def test_dsn_rendered_when_set(auth_client, monkeypatch):
    monkeypatch.setenv('SENTRY_BROWSER_DSN', DSN)
    html = _page(auth_client)
    assert DSN in html
    assert 'Sentry.init({' in html


def test_set_user_never_sends_email(auth_client, monkeypatch):
    monkeypatch.setenv('SENTRY_BROWSER_DSN', DSN)
    html = _page(auth_client)
    block = html[html.index('Sentry.setUser'):]
    block = block[:block.index('});')]
    assert 'email' not in block
    assert 'test@example.com' not in html[html.index('Sentry.init({'):html.index('__carracSentryReady')]


def test_no_baked_in_dsn_anywhere():
    # Any DSN-shaped URL (32-hex public key before the host) is a baked-in DSN.
    bad = re.compile(r'https?://[0-9a-f]{32}@')
    hits = []
    for base in ('templates', 'static/js', 'carrac'):
        for p in (ROOT / base).rglob('*'):
            if p.is_file() and p.suffix in ('.html', '.js', '.py') and 'vendor' not in p.parts:
                if bad.search(p.read_text(errors='ignore')):
                    hits.append(str(p.relative_to(ROOT)))
    assert not hits, hits


@pytest.fixture
def tunnel(app, monkeypatch):
    import carrac
    monkeypatch.setattr(carrac, '_SENTRY_DSN', SERVER_DSN)
    app.config['WTF_CSRF_ENABLED'] = False
    return app.test_client()


def _post(c, body):
    return c.post('/api/error-tunnel', data=body, content_type='application/x-sentry-envelope')


def test_tunnel_404_when_browser_dsn_unset(tunnel, monkeypatch):
    import requests
    monkeypatch.delenv('SENTRY_BROWSER_DSN', raising=False)
    monkeypatch.setattr(requests, 'post', lambda *a, **k: pytest.fail('forwarded'))
    assert _post(tunnel, _envelope()).status_code == 404


def test_tunnel_404_when_server_dsn_unset(tunnel, monkeypatch):
    import carrac
    import requests
    monkeypatch.setenv('SENTRY_BROWSER_DSN', DSN)
    monkeypatch.setattr(carrac, '_SENTRY_DSN', None)
    monkeypatch.setattr(requests, 'post', lambda *a, **k: pytest.fail('forwarded'))
    assert _post(tunnel, _envelope()).status_code == 404


def test_tunnel_forwards_to_configured_host(tunnel, monkeypatch):
    import requests
    monkeypatch.setenv('SENTRY_BROWSER_DSN', DSN)
    calls = []
    resp = MagicMock(status_code=200)
    monkeypatch.setattr(requests, 'post', lambda url, **k: calls.append(url) or resp)
    assert _post(tunnel, _envelope()).status_code == 200
    assert calls == ['http://bugsink.internal:8000/api/7/envelope/']


@pytest.mark.parametrize('dsn', [
    'https://abc123pubkey@evil.example.test/7',   # other host
    'https://abc123pubkey@errors.example.test/9',  # other project
    'https://otherkey@errors.example.test/7',      # other key
    None,                                          # no dsn header
])
def test_tunnel_rejects_foreign_envelope(tunnel, monkeypatch, dsn):
    import requests
    monkeypatch.setenv('SENTRY_BROWSER_DSN', DSN)
    monkeypatch.setattr(requests, 'post', lambda *a, **k: pytest.fail('forwarded'))
    assert _post(tunnel, _envelope(dsn)).status_code == 400
