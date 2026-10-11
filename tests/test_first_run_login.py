"""A stranger can reach a logged-in session, and only that stranger.

A fresh local install must not be a dead end at the login screen, a dead
end that would arrive AFTER the user had done everything right: `/register` is
open, so the account was created, and then non-admin login was blocked
pending a verification email that needs SMTP credentials a newcomer does not
have. The account could never be used. The one working route — set
`ADMIN_EMAIL` to the address you are about to register with — must be
documented in `.env.example` (not `ADMIN_USER` / `ADMIN_PASSWORD`, which no
auth path reads).

This adds NO new bypass: the `ADMIN_EMAIL` account is already auto-verified at
``carrac/auth/routes.py`` ("Admin users are auto-verified"), so the only gap
is discoverability. That matters for the security posture —
there is no "skip verification when not production" branch to get wrong, and
so the gate below is unconditional by construction rather than by a mode
check that could be true on a real deployment.

These tests drive the REAL /register and /login routes over HTTP. Nothing
monkeypatches the gate under test; the only thing patched is the ADMIN_EMAIL
*configuration*, which is what an operator sets in .env.
"""



ADMIN = 'owner@example.com'
STRANGER = 'stranger@example.com'
PASSWORD = 'Qm7&vTzp!Rk4wLx'


def _set_admin_email(monkeypatch, value):
    """Point both readers of ADMIN_EMAIL at `value`.

    `carrac/auth/routes.py` reads it per-request via os.getenv; `user_db`
    binds it once at import. An operator setting it in .env moves both, so a
    test that moved only one would be testing a state that cannot occur.
    """
    import user_db as _ud
    monkeypatch.setenv('ADMIN_EMAIL', value)
    monkeypatch.setattr(_ud, 'ADMIN_EMAIL', value)


def _register(client, email):
    return client.post('/register', data={
        'email': email,
        'password': PASSWORD,
        'password_confirm': PASSWORD,
        'display_name': 'Test Person',
        'agree_terms': 'on',
        'age_attested': 'on',
    }, follow_redirects=False)


def _login(client, email):
    return client.post('/login', data={'email': email, 'password': PASSWORD},
                       follow_redirects=False)


class TestTheStrangerCanGetIn:
    """From a fresh clone, with no SMTP and no undocumented variable."""

    def test_the_admin_email_account_can_log_in_immediately(
            self, app, client, monkeypatch):  # noqa: ARG002
        _set_admin_email(monkeypatch, ADMIN)

        resp = _register(client, ADMIN)
        assert resp.status_code in (301, 302), (
            f'registration did not succeed: {resp.status_code} '
            f'{resp.data[:300]!r}')

        from user_db import user_db as udb
        user = udb.get_user_by_email(ADMIN)
        assert user, 'ADMIN_EMAIL registration created no user row'
        assert udb.is_email_verified(user['id']), (
            'the ADMIN_EMAIL account was not pre-verified, so the documented '
            'first-run path in .env.example cannot work without SMTP'
        )

        resp = _login(client, ADMIN)
        assert resp.status_code == 302, (
            f'ADMIN_EMAIL login was refused: {resp.status_code} '
            f'{resp.data[:300]!r}')
        with client.session_transaction() as sess:
            assert sess.get('user_id') == user['id']


class TestTheGateStillHolds:
    """Verification is still enforced, proven by exercising it."""

    def test_an_unverified_stranger_cannot_log_in(
            self, app, client, monkeypatch):  # noqa: ARG002
        """Show it failing: the convenience above must not have opened a door
        for everyone else. This is the guard doing its job."""
        _set_admin_email(monkeypatch, ADMIN)

        resp = _register(client, STRANGER)
        assert resp.status_code == 200, (
            'a non-admin registration should land on the "check your email" '
            f'page, not redirect: {resp.status_code}')

        from user_db import user_db as udb
        user = udb.get_user_by_email(STRANGER)
        assert user, 'registration created no user row'
        assert not udb.is_email_verified(user['id']), (
            'a non-admin address was auto-verified — the verification gate '
            'has been defeated for everyone, not just the operator'
        )

        resp = _login(client, STRANGER)
        with client.session_transaction() as sess:
            assert not sess.get('user_id'), (
                'an UNVERIFIED non-admin obtained a session: the email '
                'verification gate is not being enforced'
            )

    def test_the_gate_does_not_depend_on_debug_or_testing_mode(self, app):
        """The rejected option 2 was "skip verification when not in
        production". Assert no such branch exists: the only exemption in the
        login gate is `is_admin`, so there is no mode flag that could be
        wrong on a real deployment."""
        import inspect
        from carrac.auth import routes as auth_routes

        src = inspect.getsource(auth_routes)
        marker = "if not user.get('is_admin') and not user_db.is_email_verified"
        assert marker in src, (
            'the login verification gate moved or changed shape — re-read it '
            'and update this test deliberately, do not delete the assertion'
        )

        # Positive control: this file really does contain mode flags elsewhere,
        # so "no mode flag near the gate" is a finding and not a blind grep.
        assert 'is_admin' in src

        line_no = next(i for i, ln in enumerate(src.splitlines())
                       if marker in ln)
        window = '\n'.join(src.splitlines()[line_no - 3:line_no + 3])
        for flag in ('DEBUG', 'TESTING', 'FLASK_ENV', 'is_production'):
            assert flag not in window, (
                f'{flag} appears beside the verification gate: the gate is now '
                f'mode-dependent, which is exactly what this test rejects\n'
                f'{window}'
            )


class TestTheExampleEnvTellsTheTruth:
    """The file a newcomer copies must not document dead settings."""

    def test_env_example_documents_admin_email_and_not_the_dead_pair(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        text = (root / '.env.example').read_text()

        assert 'ADMIN_EMAIL=' in text, (
            'ADMIN_EMAIL is the variable that actually confers admin and '
            'unblocks first-run login; it must be in .env.example'
        )
        for dead in ('ADMIN_USER=', 'ADMIN_PASSWORD='):
            assert dead not in text, (
                f'{dead} is read by no auth path — documenting it sends a '
                f'newcomer down a login route that cannot work'
            )
        # The two compose REQUIRES with no default; without them
        # `docker compose up` fails before a container starts.
        for required in ('POSTGRES_PASSWORD=', 'REDIS_PASSWORD='):
            assert required in text, (
                f'{required} has no default in docker-compose.yml, so a '
                f'stranger copying this file cannot start the stack'
            )
