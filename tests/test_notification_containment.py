"""The guard on the guard.

A test suite must never push a fake "General Report BLOCKED" to the live
operator topic while reporting green. The containment lives in tests/conftest.py; these tests exist so that deleting
or weakening it fails loudly instead of silently restoring the leak.

An alarm nobody can see fail is decoration. These are its positive controls.
"""
import os



# ── Layer 1: the destination is neutered before any module reads it ────────

def test_ntfy_base_points_at_the_discard_port():
    """carrac/notifications/ntfy.py captures NTFY_BASE at import time, so this
    has to be true before the first app import, not merely during a test."""
    from carrac.notifications import ntfy as ntfy_mod
    assert ntfy_mod.NTFY_BASE == 'http://127.0.0.1:9', (
        f"NTFY_BASE is {ntfy_mod.NTFY_BASE!r} — a test run could reach a real "
        "ntfy server. See the notification-containment block at the top of tests/conftest.py."
    )


def test_ntfy_topic_is_not_the_operator_topic():
    """morning_engine/config.py has had NO default for NTFY_TOPIC since
    the fact that the value is a bearer credential), so the only way a real
    topic reaches a test process is through the environment — and conftest
    forces the suite's own throwaway value there before the first import."""
    from morning_engine.config import (
        NTFY_CARRAC_TOPIC, NTFY_MARKET_TOPIC, NTFY_TOPIC,
    )
    # Every destination Carrac can post to, not just the original one. The
    # split into ops/market topics adds two more ways for a test run to reach
    # a real subscriber, and a containment guard that only knows one name
    # is exactly the guard that cannot see what it is guarding.
    for name, value in (
        ('NTFY_TOPIC', NTFY_TOPIC),
        ('NTFY_CARRAC_TOPIC', NTFY_CARRAC_TOPIC),
        ('NTFY_MARKET_TOPIC', NTFY_MARKET_TOPIC),
    ):
        assert value == os.environ[name], name
        assert value.startswith('carrac-test-'), f'{name}={value!r}'


def test_telegram_credentials_are_blank():
    """The suite may run from a shell that has sourced a local env file with real credentials."""
    from morning_engine.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    assert TELEGRAM_BOT_TOKEN == ''
    assert TELEGRAM_CHAT_ID == ''


# ── Layer 2: the senders are recorders, and a send is visible ──────────────

def test_send_ntfy_is_intercepted_not_executed(notifications):
    """Positive control: a send routed through the canonical sender lands in
    the recorder and never touches the network."""
    from carrac.notifications import send_ntfy

    assert send_ntfy("body", title="probe") is True
    assert len(notifications) == 1
    # The package re-export is patched independently of the definition, so the
    # label names whichever binding the caller reached.
    assert notifications[0]['sender'].endswith('send_ntfy')
    assert notifications[0]['kwargs']['title'] == 'probe'


def test_lazy_import_of_sender_still_hits_the_recorder(notifications):
    """The codebase's dominant idiom is a function-local
    `from carrac.notifications.ntfy import send_ntfy` (auth/routes.py,
    admin/routes.py, circuit_breaker.py, stooq.py, quant/collectors/base.py).
    Patching the definition must therefore reach them at call time."""
    def _like_circuit_breaker_does():
        from carrac.notifications.ntfy import send_ntfy
        return send_ntfy(body="Circuit breaker OPEN: probe", title="alert")

    assert _like_circuit_breaker_does() is True
    assert len(notifications) == 1


def test_telegram_sender_is_intercepted(notifications):
    from carrac.notifications import send_telegram_message

    send_telegram_message("hello")
    assert len(notifications) == 1
    assert notifications[0]['sender'].endswith('send_telegram_message')
    assert notifications[0]['args'] == ('hello',)


def test_real_sender_opt_out_restores_the_genuine_function(
        real_notification_senders, notifications):
    """`real_notification_senders` must hand back the ACTUAL send_ntfy — a
    fixture that quietly no-ops would let the header-encoding tests in
    test_breaker_notify.py assert against a stub and pass for the wrong
    reason."""
    from unittest.mock import MagicMock, patch

    from carrac.notifications.ntfy import send_ntfy

    with patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        assert send_ntfy("body", title="⚡ real", topic="t-test") is True

    # Only the real implementation builds RFC-2047 headers and posts.
    assert mock_post.call_count == 1
    assert mock_post.call_args.kwargs["headers"]["Title"].startswith("=?UTF-8?B?")
    # ...and it went to the discard port, not a real server.
    assert mock_post.call_args[0][0].startswith("http://127.0.0.1:9/")
    # The recorder saw nothing, because the real function ran instead.
    assert notifications == []


# ── The failure mode itself ────────────────────────────────────────────────

def test_unopted_send_is_reported_as_a_failure(pytestconfig):
    """The guard's teardown must FAIL a test that sends without opting in.

    Asserted structurally rather than by running a nested pytest session: the
    conftest fixture is what enforces it, and this pins the two things a future
    edit could quietly drop — that it calls pytest.fail, and that the escape
    hatch is the `notifications` fixture name.
    """
    import inspect

    from tests import conftest  # noqa: F401  (import for source access)

    src = inspect.getsource(conftest._no_real_notifications)
    assert 'pytest.fail' in src, (
        "the notification guard no longer fails the test — it would go back to "
        "recording leaks silently"
    )
    assert "'notifications' not in request.fixturenames" in src, (
        "the opt-in check changed shape; every send would now be allowed"
    )


def test_every_sender_in_the_codebase_is_covered():
    """A new sender added to carrac/notifications/ must be added to the
    conftest target list, or it is uncontained. This is the 'what calls this'
    question applied to the guard itself.
    """
    import carrac.notifications as pkg
    from tests import conftest

    covered = {attr for _, attr in conftest._NOTIFY_TARGETS_EAGER}
    exported = set(pkg.__all__)
    missing = exported - covered
    assert not missing, (
        f"carrac.notifications exports {sorted(missing)} with no notification "
        "containment. Add it to _NOTIFY_TARGETS_EAGER in tests/conftest.py."
    )
