"""Verifying an API key must stamp ``api_keys.last_used_at``.

The stamp existed but never landed: a ``from datetime import datetime`` inside
the expiry branch made ``datetime`` function-local, so every key WITHOUT an
expiry (the common case) hit an unbound local at the UPDATE, and the bare
``except: pass`` around it hid that. Real SQLite user_db,
nothing under test is monkeypatched.
"""

from datetime import datetime, timedelta

from carrac.apikeys.auth import generate_api_key, hash_api_key, get_key_suffix


def _make_key(udb, expires_at=None):
    uid = udb.create_user('lu@example.com', 'pw-1234567', tos_accepted=True)
    assert uid
    token = generate_api_key()
    kid = udb.create_api_key(uid, hash_api_key(token), get_key_suffix(token),
                             expires_at=expires_at)
    assert kid
    return uid, kid, token


def _last_used(udb, uid, kid):
    for k in udb.list_api_keys(uid):
        if k['id'] == kid:
            return k['last_used_at']
    raise AssertionError('key not listed')


def test_verify_stamps_last_used_for_key_without_expiry(app):
    import user_db as udb_mod
    udb = udb_mod.user_db
    with app.app_context():
        uid, kid, token = _make_key(udb)
        assert _last_used(udb, uid, kid) is None
        assert udb.verify_api_key_token(hash_api_key(token))
        stamped = _last_used(udb, uid, kid)
    assert stamped, 'last_used_at still NULL after a verified request'
    dt = datetime.fromisoformat(str(stamped).split('+')[0])
    assert datetime.now() - dt < timedelta(minutes=1)


def test_stamp_is_throttled_within_the_hour(app):
    import user_db as udb_mod
    udb = udb_mod.user_db
    with app.app_context():
        uid, kid, token = _make_key(udb)
        assert udb.verify_api_key_token(hash_api_key(token))
        first = _last_used(udb, uid, kid)
        assert udb.verify_api_key_token(hash_api_key(token))
        second = _last_used(udb, uid, kid)
    assert first and second == first, 'hot path re-wrote the stamp inside the throttle window'


def test_stale_stamp_advances(app):
    import user_db as udb_mod
    from carrac.db_pool import adapt_query
    udb = udb_mod.user_db
    with app.app_context():
        uid, kid, token = _make_key(udb)
        old = (datetime.now() - timedelta(hours=2)).isoformat(sep=' ')
        conn = udb._get_conn()
        conn.execute(adapt_query('UPDATE api_keys SET last_used_at = ? WHERE id = ?'), (old, kid))
        conn.commit()
        assert udb.verify_api_key_token(hash_api_key(token))
        stamped = _last_used(udb, uid, kid)
    assert str(stamped) != old
    dt = datetime.fromisoformat(str(stamped).split('+')[0])
    assert datetime.now() - dt < timedelta(minutes=1)


def test_failed_stamp_is_logged_at_error_not_swallowed(app, monkeypatch, caplog):
    """A bare ``except: pass`` around the UPDATE hides failures. Break the stamp on
    purpose and prove the failure now reaches the ERROR log (the error tracker)."""
    import logging
    import user_db as udb_mod
    udb = udb_mod.user_db
    monkeypatch.setattr(udb_mod, '_last_used_stamp_sql',
                        lambda kid: ('UPDATE no_such_table SET x = ? WHERE id = ?', (1, kid)))
    with app.app_context():
        uid, kid, token = _make_key(udb)
        with caplog.at_level(logging.ERROR, logger=udb_mod.logger.name):
            assert udb.verify_api_key_token(hash_api_key(token)), 'auth must still succeed'
    errs = [r for r in caplog.records if 'last_used_at stamp failed' in r.getMessage()]
    assert errs and errs[0].levelno == logging.ERROR
