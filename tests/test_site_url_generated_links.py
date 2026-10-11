"""Every link or address the app generates follows the instance's own SITE_URL.

A self-hosted instance must never send its users to the hosted product's
domain (which may lapse and be re-registered by someone else). Each test sets
SITE_URL to a neutral origin and asserts it appears and the hosted domain does not.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASE = 'https://example.test'
OLD = 'carrac.cc'


@pytest.fixture(autouse=True)
def _own_site_url(monkeypatch):
    monkeypatch.setenv('SITE_URL', BASE)
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)


def _clean(text):
    assert BASE in text or 'example.test' in text
    assert OLD not in text


def test_helper_precedence(monkeypatch):
    from carrac.site_url import site_base_url, site_host
    assert site_base_url() == BASE
    assert site_host() == 'example.test'
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://pub.test/')
    assert site_base_url() == 'https://pub.test'
    monkeypatch.delenv('PUBLIC_BASE_URL')
    monkeypatch.delenv('SITE_URL')
    assert site_base_url() == 'http://localhost:5002'


def test_invite_email_links(monkeypatch):
    import carrac.email as em
    got = {}
    monkeypatch.setattr(em, 'send_email', lambda to, s, h, text_body=None: got.update(h=h, t=text_body) or True)
    assert em.send_invite_email('a@b.test', 'WAVE1234')
    assert f'{BASE}/register?code=WAVE1234' in got['h']
    _clean(got['h'])
    _clean(got['t'])


def test_send_email_refuses_loudly_without_sender(monkeypatch, caplog):
    import carrac.email as em
    monkeypatch.setattr(em, 'BREVO_USER', 'u')
    monkeypatch.setattr(em, 'BREVO_PASS', 'p')
    monkeypatch.setattr(em, 'BREVO_FROM', '')
    with caplog.at_level('ERROR', logger='carrac.email'):
        assert em.send_email('a@b.test', 's', '<p>x</p>') is False
    assert 'BREVO_FROM_EMAIL not configured' in caplog.text


def test_no_default_sender_address():
    from carrac.config import Config
    assert Config.BREVO_FROM_EMAIL == ''


def test_auth_emails_use_site_url(app, monkeypatch):
    import carrac.email as em
    from carrac.auth import routes
    sent = []
    monkeypatch.setattr(em, 'send_email', lambda to, s, h, text_body=None: sent.append((h, text_body)) or True)
    with app.test_request_context():
        routes._send_welcome_email('a@b.test')
        routes._send_verification_email('a@b.test', 'tok')
        routes._send_password_reset_email('a@b.test', 'tok')
    assert len(sent) == 3
    for html, text in sent:
        _clean(html)
        _clean(text)
    assert f'href="{BASE}"' in sent[0][0]


def test_report_ready_push_body(monkeypatch):
    import push_notifications as pn
    import requests
    import user_db as user_db_module
    posts = []
    monkeypatch.setattr(pn, '_resolve_user_id', lambda u: 1)

    # notify_user_report_ready does `from user_db import user_db` at call
    # time, so swap that module attribute for a stub. Patching the class or
    # the shared instance is not enough: other tests replace or wrap the
    # instance, and whichever patch they leave behind decides the outcome.
    class _Prefs:
        def get_preferences(self, uid):
            return {'notifications': {'ntfy': {'enabled': True, 'url': 'https://ntfy.test/t'}}}
    monkeypatch.setattr(user_db_module, 'user_db', _Prefs())
    monkeypatch.setattr(requests, 'post', lambda url, **kw: posts.append(kw) or None)
    pn.notify_user_report_ready('u', report_id=5)
    body = posts[0]['data'].decode()
    assert f'{BASE}/report/morning' in body
    assert OLD not in body


def test_curl_surfaces(monkeypatch):
    from carrac.curl_charts import content
    text = '\n'.join([content.funnel_footer(True), content.help_page(), content.mcp_page(),
                      content.register_page(True), content.assets_page(['BTC']),
                      content.unknown_symbol('x', True), content.usage_error('e', True),
                      content.rate_limited(True), content.register_url(),
                      content.register_tui_url(), content.register_qr_url()])
    _clean(text)
    assert content.register_url().startswith(f'{BASE}/register?')
    assert f'{BASE}/mcp' in content.mcp_page()


def test_openapi_servers():
    from carrac.openapi import build_spec, reset_spec_cache
    # build_spec() caches per process; another test may have built it under a
    # different SITE_URL. Rebuild under ours, and don't leak ours onward.
    reset_spec_cache()
    try:
        spec = build_spec()
    finally:
        reset_spec_cache()
    assert spec['servers'][0]['url'] == BASE
    assert OLD not in str(spec['servers']) + str(spec['info']['contact'])


def test_public_pages_snippets(client):
    for path in ('/guide', '/integrations'):
        html = client.get(path).data.decode()
        assert f'{BASE}/mcp' in html or 'example.test/btc' in html, path
        # canonical/og metadata may still name the product site; connect
        # snippets (mcp endpoint, curl examples) must not.
        assert 'app.carrac.cc' not in html, path
        assert 'curl carrac.cc' not in html, path


def test_mcp_guide_section_filled(monkeypatch):
    from carrac.mcp.tools import guide
    monkeypatch.setattr(guide, '_GUIDE_LOADED', False)
    monkeypatch.setattr(guide, '_GUIDE_SECTIONS', {})
    guide._load_guide()
    blob = ' '.join(s['text'] for s in guide._GUIDE_SECTIONS.values())
    assert '{{ site_url }}' not in blob and '{{ site_host }}' not in blob
    assert f'{BASE}/mcp' in blob
    assert 'app.carrac.cc' not in blob


def test_settings_page_connect_urls(auth_client):
    resp = auth_client.get('/settings')
    html = resp.data.decode()
    assert resp.status_code == 200
    assert f'{BASE}/mcp' in html
    assert 'app.carrac.cc' not in html


def test_mcp_tier_denial_message(monkeypatch):
    src = (ROOT / 'carrac/mcp/protocol.py').read_text()
    assert OLD not in src


# Code that builds links must not hardcode the hosted domain. Allowed: legal
# identity defaults, deleted-account addresses and support contacts in user_db,
# the host-based redirect for requests that arrive on the hosted domain, an
# outbound User-Agent, and prose/comment mentions.
_ALLOWED = {'user_db.py', 'carrac/legal_entity.py', 'carrac/__init__.py',
            'quant/collectors/btc_network.py'}


def test_no_hardcoded_hosted_domain_in_generated_links():
    offenders = []
    for base in ('carrac', 'morning_engine', 'quant', '.'):
        pattern = '*.py' if base == '.' else '**/*.py'
        paths = ROOT.glob(pattern) if base == '.' else (ROOT / base).glob(pattern)
        for p in paths:
            rel = str(p.relative_to(ROOT))
            if rel in _ALLOWED or rel.startswith(('tests', '.venv', 'scripts', 'alembic', 'sdk')):
                continue
            for n, line in enumerate(p.read_text(errors='ignore').splitlines(), 1):
                code = line.split('#', 1)[0]
                if re.search(r'https?://[\w.-]*carrac\.cc|[\'"]carrac\.cc', code):
                    offenders.append(f'{rel}:{n}')
    assert not offenders, offenders


def test_landing_and_marketing_meta_follow_site_url(client):
    html = client.get('/').data.decode()
    assert f'<link rel="canonical" href="{BASE}/"' in html
    assert f'{BASE}/static/og-dashboard.png' in html
    assert OLD not in html.replace('Carrac.cc', '')


@pytest.mark.parametrize('path', ['/privacy', '/terms'])
def test_legal_pages_contact_is_operators(client, monkeypatch, path):
    monkeypatch.delenv('SUPPORT_EMAIL', raising=False)
    html = client.get(path).data.decode()
    assert '[operator: set SUPPORT_EMAIL]' in html
    assert 'support@carrac.cc' not in html and 'EspoCRM' not in html
    assert f'{BASE}{path}' in html  # canonical
    monkeypatch.setenv('SUPPORT_EMAIL', 'help@example.test')
    html = client.get(path).data.decode()
    assert 'help@example.test' in html and 'operator: set' not in html
