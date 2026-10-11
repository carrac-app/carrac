"""alert_create_from_drawing must not read any user's drawing by id.

carrac/mcp/tools/bridges.py must call ``db.get_drawing_by_id`` with the
caller's ``user_id``. The helper's docstring (carrac/content_db.py)
says callers that need to verify ownership MUST pass it — every other
drawing-touching MCP tool does. Omitting it lets a member-tier caller
walk the autoincrement id space and read back another user's asset and
price levels in the created alert objects.

carrac/mcp/tools/context.py has the same ownership rule, which is why this
file exists next to tests/test_idor_drawing_link.py rather than inside
test_mcp_bridges.py.

Only the *collaborators* are stubbed here (the notification channel and the
alert writer). The drawing lookup — the unit under test — runs against a real
ContentDatabase, so a regression cannot hide by mocking the code under test.
"""
import pytest

from carrac.mcp.protocol import ToolError

VICTIM = 202
ATTACKER = 101

VICTIM_PRICE = 68123.5
VICTIM_UPPER = 69000.0


@pytest.fixture
def content_db(tmp_path, monkeypatch):
    """Fresh ContentDatabase on a tmp SQLite file (mirrors test_idor_drawing_link)."""
    import carrac.db_pool as _cdb
    _cdb._db_type = None
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    from carrac.content_db import ContentDatabase
    return ContentDatabase(db_path=str(tmp_path / 'idor_bridges_drawings.db'))


@pytest.fixture
def bridges_env(content_db, monkeypatch):
    """Bind the process-wide get_content_db() singleton to the tmp database and
    stub the two push_notifications collaborators the handler reaches for.

    Returns the list that records every create_alert call, so a test can assert
    the handler never got as far as writing an alert.
    """
    import carrac.content_db as _content_db_mod
    import push_notifications

    previous = _content_db_mod._db_instance
    _content_db_mod._db_instance = content_db

    created = []

    def _fake_create_alert(**kwargs):
        created.append(kwargs)
        return {'id': len(created), **kwargs}

    def _fake_create_trendline_alert(**kwargs):
        created.append(kwargs)
        return {'id': len(created), **kwargs}

    monkeypatch.setattr(push_notifications, 'user_has_notification_channel',
                        lambda uid: True)
    monkeypatch.setattr(push_notifications, 'create_alert', _fake_create_alert)
    monkeypatch.setattr(push_notifications, 'create_trendline_alert',
                        _fake_create_trendline_alert)

    yield created
    _content_db_mod._db_instance = previous


def _seed_line(content_db, user_id, price_level):
    return content_db.save_drawing(
        asset='BTC', timeframe='1D', drawing_type='horizontal_line',
        price_level=price_level, user_id=user_id,
    )


def _seed_zone(content_db, user_id, lower, upper):
    return content_db.save_drawing(
        asset='ETH', timeframe='4h', drawing_type='zone',
        price_level=lower, price_level_upper=upper, user_id=user_id,
    )


class TestAlertCreateFromDrawingOwnership:
    def test_cannot_read_another_users_horizontal_line(
            self, bridges_env, content_db, bind_mcp_user):
        """The core case: attacker guesses the victim's drawing id."""
        from carrac.mcp.tools.bridges import alert_create_from_drawing

        victim_id = _seed_line(content_db, VICTIM, VICTIM_PRICE)
        bind_mcp_user(uid=ATTACKER)

        with pytest.raises(ToolError) as exc:
            alert_create_from_drawing({'drawing_id': victim_id})

        # Must read as a plain miss — not "exists but forbidden", which would
        # still confirm the id is real.
        assert 'not found' in str(exc.value).lower()
        # And nothing may have been written on the back of the victim's level.
        assert bridges_env == []

    def test_victims_price_never_reaches_the_attacker(
            self, bridges_env, content_db, bind_mcp_user):
        """Belt and braces: the victim's number must not appear anywhere in
        whatever comes back, error text included."""
        from carrac.mcp.tools.bridges import alert_create_from_drawing

        victim_id = _seed_zone(content_db, VICTIM, VICTIM_PRICE, VICTIM_UPPER)
        bind_mcp_user(uid=ATTACKER)

        with pytest.raises(ToolError) as exc:
            alert_create_from_drawing({'drawing_id': victim_id})

        blob = str(exc.value)
        assert str(VICTIM_PRICE) not in blob
        assert str(VICTIM_UPPER) not in blob
        assert bridges_env == []

    def test_owner_can_still_create_alerts_from_their_own_drawing(
            self, bridges_env, content_db, bind_mcp_user):
        """The fix must not break the feature — this is the branch that has to
        keep working, and it is why the check is a scoped lookup rather than a
        blanket refusal."""
        from carrac.mcp.tools.bridges import alert_create_from_drawing

        own_id = _seed_line(content_db, ATTACKER, 12345.0)
        bind_mcp_user(uid=ATTACKER)

        out = alert_create_from_drawing({'drawing_id': own_id})

        assert out['drawing_id'] == own_id
        assert out['asset'] == 'BTC'
        assert out['count'] == 2  # above + below
        assert len(bridges_env) == 2
        assert all(c['user_id'] == str(ATTACKER) for c in bridges_env)
        assert all(c['price'] == 12345.0 for c in bridges_env)

    def test_missing_drawing_id_is_indistinguishable_from_someone_elses(
            self, bridges_env, content_db, bind_mcp_user):
        """A wholly absent id and another user's id must produce the same
        answer, or the tool is still an existence oracle."""
        from carrac.mcp.tools.bridges import alert_create_from_drawing

        victim_id = _seed_line(content_db, VICTIM, VICTIM_PRICE)
        bind_mcp_user(uid=ATTACKER)

        with pytest.raises(ToolError) as other_user:
            alert_create_from_drawing({'drawing_id': victim_id})
        with pytest.raises(ToolError) as absent:
            alert_create_from_drawing({'drawing_id': victim_id + 99999})

        assert str(other_user.value).replace(str(victim_id), 'X') == \
            str(absent.value).replace(str(victim_id + 99999), 'X')
