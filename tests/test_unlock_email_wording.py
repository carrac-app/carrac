"""The account-unlock email names the product, never a hosted domain: a
self-hosted instance must not tell its users they hold an account somewhere
else."""

from __future__ import annotations

from unittest.mock import patch


def test_unlock_email_names_the_product_not_a_domain(app):
    from user_db import user_db
    from carrac.auth.routes import _send_unlock_email

    email = "unlock-wording@example.test"
    assert user_db.create_user(email, "StrongPass!123", tos_accepted=True)

    with app.test_request_context(), \
         patch("carrac.email.send_email", return_value=True) as send:
        assert _send_unlock_email(email) is True

    _to, subject, html = send.call_args.args[:3]
    text = send.call_args.kwargs["text_body"]
    for part in (subject, html, text):
        assert ".cc" not in part
    assert "your Carrac account" in html
    assert subject == "Security alert: confirm your Carrac account"
