"""A verification resend must not kill the link already in the inbox.

Reconstructed from a real request log: register,
two "resend verification" clicks within ten seconds, then the link from the
FIRST mail opened 22 s later — and answered with the "invalid or has expired"
page, because `create_email_verification_token()` marked every earlier token
used before inserting the new one. The 2-per-hour resend limit then refused a
third mail. Id 34 shows the same opening and never verified either.

Every unexpired link a user was mailed must work until one of them is used;
using one retires the rest (that half already exists in set_email_verified).
"""

import hashlib
import secrets

from user_db import user_db


def _token():
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def _new_user():
    return user_db.create_user(email=f'{secrets.token_hex(6)}@example.com',
                               password='correct-horse-battery', display_name='t')


def test_a_resend_leaves_the_first_link_valid(app):
    uid = _new_user()
    _, first_hash = _token()
    _, second_hash = _token()
    assert user_db.create_email_verification_token(uid, first_hash)
    assert user_db.create_email_verification_token(uid, second_hash)   # the resend

    first = user_db.validate_email_verification_token(first_hash)
    second = user_db.validate_email_verification_token(second_hash)

    assert first and first['user_id'] == uid, 'the link in the first mail died on resend'
    assert second and second['user_id'] == uid


def test_using_any_link_retires_all_of_them(app):
    uid = _new_user()
    _, first_hash = _token()
    _, second_hash = _token()
    user_db.create_email_verification_token(uid, first_hash)
    user_db.create_email_verification_token(uid, second_hash)

    assert user_db.set_email_verified(uid)

    assert user_db.validate_email_verification_token(first_hash) is None
    assert user_db.validate_email_verification_token(second_hash) is None
    assert user_db.is_email_verified(uid)
