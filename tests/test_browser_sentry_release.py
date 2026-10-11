"""The browser Sentry SDK must tag events with the release.

The Python SDK was given `release=APP_VERSION`; the browser init in
templates/index.html set none, so every javascript event in the error tracker carried
release '' and the weekly review could not tell a pre-promote
browser event from a post-promote one. The rendered page is the unit here —
the value must come from the same `app_version` context the footer uses.
Only the logged-in app page (index.html) loads Sentry; the landing page does not.
"""

from carrac.version import APP_VERSION


def _sentry_init_block(html: str) -> str:
    assert 'Sentry.init({' in html, 'index.html no longer inits Sentry inline'
    return html.split('Sentry.init({', 1)[1].split('});', 1)[0]


def test_browser_sentry_init_carries_the_app_version_as_release(auth_client, monkeypatch):
    monkeypatch.setenv("SENTRY_BROWSER_DSN", "https://pub@errors.example.test/2")
    resp = auth_client.get("/")
    assert resp.status_code == 200
    init = _sentry_init_block(resp.get_data(as_text=True))
    assert f'release: "{APP_VERSION}"' in init, init
