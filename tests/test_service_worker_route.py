"""The served service worker carries the running app version in its cache names."""

from carrac.version import APP_VERSION

TOKEN = '__APP_VERSION__'


def test_cache_names_carry_app_version(client):
    resp = client.get('/service-worker.js')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for name in ('carrac-v', 'carrac-static-v', 'carrac-data-v'):
        assert f"'{name}{APP_VERSION}'" in body
    assert 'placeholder' not in body
    assert TOKEN not in body


def test_headers_stay_revalidating(client):
    resp = client.get('/service-worker.js')
    assert resp.headers['Service-Worker-Allowed'] == '/'
    assert resp.headers['Content-Type'].startswith('application/javascript')
    assert 'must-revalidate' in resp.headers['Cache-Control']
