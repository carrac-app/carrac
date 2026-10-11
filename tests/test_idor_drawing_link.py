"""
TDD test: cross-user drawing-link IDOR.

Chain: carrac/plays/routes.py:link_drawing_endpoint calls
carrac/plays/models.py:link_drawing(play_id, user_id, drawing_id), which
checked the PLAY's ownership but not the DRAWING's — so user A could link
user B's private drawing_id into A's own play, then mint a public /s/<slug>
share (carrac/plays/shares.py:_freeze_drawings) that copies the victim's
drawing fields into the public snapshot.

This test drives three fixes directly (no Flask app / request layer needed):
  1. carrac/content_db.py: get_drawing_by_id(id, user_id=...) scopes
     the row lookup to that user.
  2. carrac/plays/models.py: link_drawing refuses to link a drawing that
     does not belong to the caller.
  3. carrac/plays/shares.py: _freeze_drawings drops any drawing not owned by
     the play's user (defense-in-depth, even if #2 is somehow bypassed).
"""
import pytest


@pytest.fixture
def content_db(tmp_path, monkeypatch):
    """Fresh ContentDatabase bound to a tmp SQLite file.

    Mirrors the `db` fixture in tests/test_auth_idor.py.
    """
    import carrac.db_pool as _cdb
    _cdb._db_type = None
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    from carrac.content_db import ContentDatabase
    return ContentDatabase(db_path=str(tmp_path / 'idor_link_drawings.db'))


@pytest.fixture
def plays_env(tmp_path, monkeypatch, content_db):
    """Isolate the plays table (db_pool sqlite conn) in a tmp dir, and bind
    the process-wide get_content_db() singleton to the SAME ContentDatabase
    instance the `content_db` fixture just created — so carrac.plays.models
    .link_drawing (which reaches for the real get_content_db() singleton
    internally) sees the drawings this test seeds.
    """
    import carrac.db_pool as _db_pool
    import carrac.content_db as _content_db_mod

    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    _db_pool._db_type = None
    if getattr(_db_pool._sqlite_local, 'conn', None) is not None:
        try:
            _db_pool._sqlite_local.conn.close()
        except Exception:
            pass
        _db_pool._sqlite_local.conn = None

    previous = _content_db_mod._db_instance
    _content_db_mod._db_instance = content_db
    yield
    _content_db_mod._db_instance = previous


USER_A = 101
USER_B = 202


def _make_drawing(content_db, user_id, price_level):
    return content_db.save_drawing(
        asset='BTC', timeframe='1D', drawing_type='horizontal_line',
        price_level=price_level, user_id=user_id,
    )


class TestGetDrawingByIdOwnerFilter:
    """carrac/content_db.py get_drawing_by_id(id, user_id=...)."""

    def test_no_user_id_is_backward_compatible(self, content_db):
        """Existing callers that pass only the id must keep working."""
        did = _make_drawing(content_db, USER_A, 111)
        row = content_db.get_drawing_by_id(did)
        assert row is not None
        assert row['id'] == did

    def test_matching_user_id_returns_row(self, content_db):
        did = _make_drawing(content_db, USER_A, 222)
        row = content_db.get_drawing_by_id(did, user_id=USER_A)
        assert row is not None
        assert row['id'] == did

    def test_wrong_user_id_returns_none(self, content_db):
        """Caller must not be able to read another user's drawing
        by id, even when they know/guess the id."""
        did = _make_drawing(content_db, USER_B, 333)
        row = content_db.get_drawing_by_id(did, user_id=USER_A)
        assert row is None


class TestLinkDrawingOwnershipCheck:
    """carrac/plays/models.py link_drawing must verify drawing ownership."""

    def test_link_own_drawing_succeeds(self, plays_env, content_db):
        from carrac.plays.models import play_create, link_drawing

        play = play_create(USER_A, title='Test play A')
        own_drawing_id = _make_drawing(content_db, USER_A, 42000)

        result = link_drawing(play['id'], USER_A, own_drawing_id)

        assert result is not None
        assert own_drawing_id in (result.get('linked_drawing_ids') or [])

    def test_link_other_users_drawing_is_rejected(self, plays_env, content_db):
        """User A must not be able to link user B's drawing into
        A's play — this is the exact leak chain."""
        from carrac.plays.models import play_create, get_play, link_drawing

        play = play_create(USER_A, title='Test play A')
        victims_drawing_id = _make_drawing(content_db, USER_B, 99000)

        result = link_drawing(play['id'], USER_A, victims_drawing_id)

        assert result is None

        # Belt-and-suspenders: the play's linked_drawing_ids must not have
        # been mutated by the rejected attempt.
        fresh = get_play(play['id'], USER_A)
        assert victims_drawing_id not in (fresh.get('linked_drawing_ids') or [])


class TestFreezeDrawingsOwnerFilter:
    """carrac/plays/shares.py _freeze_drawings defense-in-depth filter."""

    def test_owned_drawing_is_frozen(self, plays_env, content_db):
        from carrac.plays.shares import _freeze_drawings

        own_drawing_id = _make_drawing(content_db, USER_A, 1234)
        frozen = _freeze_drawings([own_drawing_id], owner_user_id=USER_A)
        assert len(frozen) == 1

    def test_unowned_drawing_is_dropped(self, plays_env, content_db):
        """Even if an unowned drawing_id somehow ended up in
        linked_drawing_ids, the freeze step must not leak it into the
        public snapshot."""
        from carrac.plays.shares import _freeze_drawings

        victims_drawing_id = _make_drawing(content_db, USER_B, 5678)
        frozen = _freeze_drawings([victims_drawing_id], owner_user_id=USER_A)
        assert frozen == []
