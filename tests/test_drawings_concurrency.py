"""
Tests that drawing_delete must include AND version = ? predicate
so a concurrent write that bumped the version is not silently destroyed.
"""

import pytest


@pytest.fixture
def content_db(tmp_path, monkeypatch):
    """Return a ContentDatabase backed by an isolated SQLite file."""
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    import carrac.db_pool as _dp
    _dp._db_type = None  # re-read env
    if hasattr(_dp._sqlite_local, 'conn') and _dp._sqlite_local.conn is not None:
        try:
            _dp._sqlite_local.conn.close()
        except Exception:
            pass
        _dp._sqlite_local.conn = None

    from carrac.content_db import ContentDatabase
    db_path = str(tmp_path / 'drawings.db')
    db = ContentDatabase(db_path=db_path)
    yield db

    # Cleanup
    if hasattr(_dp._sqlite_local, 'conn') and _dp._sqlite_local.conn is not None:
        try:
            _dp._sqlite_local.conn.close()
        except Exception:
            pass
        _dp._sqlite_local.conn = None


class TestDeleteDrawingVersionPredicate:
    """Delete with stale expected_version returns UPDATE_CONFLICT."""

    def test_delete_stale_version_returns_conflict_drawing_intact(self, content_db):
        """
        Scenario:
          Tab A reads drawing at version=1 and prepares to delete.
          Tab B updates the drawing first (version becomes 2).
          Tab A's delete with expected_version=1 must fail with UPDATE_CONFLICT.
          The drawing must still exist in DB at version=2.
        """
        db = content_db

        # Create a drawing (starts at version=1)
        drawing_id = db.save_drawing(
            asset='BTC',
            timeframe='1D',
            drawing_type='horizontal_line',
            price_level=50000.0,
            label='Support',
            user_id=1,
        )

        # Simulate Tab B bumping the version to 2 via a direct DB UPDATE
        conn = db._get_conn()
        conn.execute(
            'UPDATE drawings SET version = 2, label = ? WHERE id = ?',
            ('Updated by Tab B', drawing_id),
        )
        conn.commit()

        # Tab A tries to delete with stale expected_version=1
        result = db.drawing_delete(drawing_id, user_id=1, expected_version=1)

        assert result == db.UPDATE_CONFLICT, (
            f"Expected UPDATE_CONFLICT but got {result!r}"
        )

        # Drawing must still be in the DB at version=2
        # Use user_id=-1 sentinel (cross-user system read)
        rows = db.drawing_list(asset='BTC', user_id=-1)
        assert any(r['id'] == drawing_id for r in rows), (
            "Drawing was deleted despite version mismatch — concurrent edit lost"
        )
        surviving = next(r for r in rows if r['id'] == drawing_id)
        assert surviving['version'] == 2

    def test_delete_correct_version_succeeds(self, content_db):
        """Sanity check: delete with the current version must work."""
        db = content_db
        drawing_id = db.save_drawing(
            asset='ETH',
            timeframe='4H',
            drawing_type='horizontal_line',
            price_level=3000.0,
            user_id=1,
        )
        result = db.drawing_delete(drawing_id, user_id=1, expected_version=1)
        assert result == db.UPDATE_OK

        rows = db.drawing_list(asset='ETH', user_id=-1)
        assert not any(r['id'] == drawing_id for r in rows)
