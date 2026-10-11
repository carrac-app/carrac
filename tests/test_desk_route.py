"""Trader's Desk route — flag gating + auth."""


class TestDeskRoute:
    def test_404_when_flag_off(self, auth_client, monkeypatch):
        monkeypatch.delenv('DESK_ENABLED', raising=False)
        assert auth_client.get('/desk').status_code == 404

    def test_renders_when_flag_on(self, auth_client, monkeypatch):
        monkeypatch.setenv('DESK_ENABLED', '1')
        r = auth_client.get('/desk')
        assert r.status_code == 200
        assert 'Trader' in r.get_data(as_text=True)

    def test_anonymous_redirects_to_login(self, client, monkeypatch):
        monkeypatch.setenv('DESK_ENABLED', '1')
        r = client.get('/desk')
        assert r.status_code in (301, 302)
        assert 'login' in r.headers.get('Location', '')

    def test_shell_mounts_present(self, auth_client, monkeypatch):
        monkeypatch.setenv('DESK_ENABLED', '1')
        html = auth_client.get('/desk').get_data(as_text=True)
        for needle in ('id="desk-stream"', 'id="desk-grid-wrap"',
                       'id="cd-drawer"', 'data-context="desk"'):
            assert needle in html, needle


class TestDeskOwnerAccess:
    """The owner reaches the Desk while DESK_ENABLED is still off.

    DESK_ENABLED stays the GA switch — flipping it opens the Desk to everyone.
    Until then it is admin-only, so the 404 for everyone else is the guard that
    matters, not the 200.
    """

    def test_admin_reaches_desk_with_flag_off(self, admin_client, monkeypatch):
        monkeypatch.delenv('DESK_ENABLED', raising=False)
        r = admin_client.get('/desk')
        assert r.status_code == 200
        assert 'Trader' in r.get_data(as_text=True)

    def test_non_admin_still_404s_with_flag_off(self, auth_client, monkeypatch):
        monkeypatch.delenv('DESK_ENABLED', raising=False)
        assert auth_client.get('/desk').status_code == 404

    def test_anonymous_404s_with_flag_off(self, client, monkeypatch):
        """404, not a login redirect — an anonymous probe must not learn the
        route exists."""
        monkeypatch.delenv('DESK_ENABLED', raising=False)
        assert client.get('/desk').status_code == 404


class TestDeskNavLink:
    """The More-menu link must appear exactly where /desk answers.

    A link that 404s is worse than no link, so these two conditions are one
    fact expressed twice — this pins them together.
    """

    def test_admin_sees_link_with_flag_off(self, admin_client, monkeypatch):
        monkeypatch.delenv('DESK_ENABLED', raising=False)
        assert 'href="/desk"' in admin_client.get('/').get_data(as_text=True)

    def test_non_admin_sees_no_link_with_flag_off(self, auth_client, monkeypatch):
        monkeypatch.delenv('DESK_ENABLED', raising=False)
        assert 'href="/desk"' not in auth_client.get('/').get_data(as_text=True)

    def test_non_admin_sees_link_with_flag_on(self, auth_client, monkeypatch):
        monkeypatch.setenv('DESK_ENABLED', '1')
        assert 'href="/desk"' in auth_client.get('/').get_data(as_text=True)


class TestPostLoginRedirect:
    def test_desk_flag_on(self, app, monkeypatch):
        monkeypatch.setenv('DESK_ENABLED', '1')
        from carrac.auth.routes import _post_login_redirect
        with app.test_request_context():
            assert _post_login_redirect().headers['Location'].endswith('/desk')

    def test_desk_flag_off(self, app, monkeypatch):
        monkeypatch.delenv('DESK_ENABLED', raising=False)
        from carrac.auth.routes import _post_login_redirect
        with app.test_request_context():
            assert '/desk' not in _post_login_redirect().headers['Location']
