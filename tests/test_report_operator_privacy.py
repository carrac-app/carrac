"""Operator-private report text and narration never reach a non-admin caller.

The morning, afternoon and weekly reports carry the operator's own positions
and trades ("Action Dashboard", "Play Review"); the morning summary is the
operator's personal executive summary; every stored narration (audio_script
and the report_<id>.mp3 spoken from it) is written from that full text. These
tests drive the real Flask routes against a real ContentDatabase, as a member
session, a free session, a per-user API key, the admin session and the
internal key the report engine uses.
"""

from __future__ import annotations

import os

import pytest

_INTERNAL = {'X-Internal-Key': 'test-api-key-for-testing'}

_MORNING = (
    "## The Story\nMacro narrative; nothing operator-private here.\n\n"
    "## Action Dashboard\n"
    "BTC Position Risk: short entered at $75,736 with stop $79,872.\n\n"
    "## Market Snapshot\n| Asset | Price |\n| BTC | 71287 |\n"
)
_WEEKLY = (
    "## 1. Week in Review\nBTC opened the week at $73,413.\n\n"
    "## 5. Play Review\n- Play #18 OIL Supply-Shock: thesis intact, re-armed.\n\n"
    "## 7. Next Week Setup\nWatch the 59.5k shelf.\n"
)
_PRIVATE_SUMMARY = 'Operator summary: hold the BTC short, stop $79,872.'
_PRIVATE_SCRIPT = 'Your BTC short from $75,736 is working; keep the stop at $79,872.'


@pytest.fixture
def db(app):  # noqa: ARG001 — app fixture sets DATA_DIR before the singleton binds
    import carrac.content_db as cdb

    cdb._db_instance = None
    yield cdb.get_content_db()
    cdb._db_instance = None


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    """_audio_dir() is repo-relative; keep mp3 fixtures out of the tree."""
    import carrac.reports.routes as rr

    d = tmp_path / 'audio'
    d.mkdir()
    monkeypatch.setattr(rr, '_audio_dir', lambda: str(d))
    return d


def _seed(db, report_type, content):
    rid = db.save_report(content=content, assets_included=[], generation_time_ms=0,
                         report_type=report_type, audio_script=_PRIVATE_SCRIPT)
    conn = db._get_conn()
    conn.execute('UPDATE reports SET summary = ? WHERE id = ?', (_PRIVATE_SUMMARY, rid))
    conn.commit()
    return rid


def _write_mp3(audio_dir, name, payload=b'ID3public-narration'):
    with open(os.path.join(str(audio_dir), name), 'wb') as fh:
        fh.write(payload)


def _login(client, tier, user_id=99):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['email'] = f'{tier}@example.com'
        sess['is_admin'] = tier == 'admin'
        sess['tier'] = tier


@pytest.fixture
def user_key(app):  # noqa: ARG001
    """A real per-user API key minted for a free-tier user."""
    from carrac.apikeys.auth import generate_api_key, hash_api_key
    from user_db import user_db

    uid = user_db.create_user('keyholder@example.com', 'pw-Long-enough-1')
    assert uid
    key = generate_api_key()
    assert user_db.create_api_key(uid, hash_api_key(key), key[-4:])
    return {'X-API-Key': key}


def _assert_no_private_text(rows):
    assert rows, 'the route returned no reports'
    for r in rows:
        blob = ' '.join(str(r.get(k) or '') for k in ('content', 'summary', 'audio_script'))
        assert 'Action Dashboard' not in blob
        assert 'Play Review' not in blob
        assert '75,736' not in blob
        assert '79,872' not in blob
        assert 'Play #18' not in blob


class TestRecentReportsText:
    @pytest.mark.parametrize('tier', ['member', 'free'])
    def test_session_non_admin_gets_no_operator_sections(self, client, db, tier):
        _seed(db, 'morning', _MORNING)
        _login(client, tier)
        resp = client.get('/api/report/morning/recent?type=morning')
        assert resp.status_code == 200
        rows = resp.get_json()
        _assert_no_private_text(rows)
        # The operator-neutral parts still arrive.
        assert 'Market Snapshot' in rows[0]['content']
        assert '71287' in rows[0]['content']

    def test_per_user_api_key_gets_no_operator_sections(self, client, db, user_key):
        _seed(db, 'morning', _MORNING)
        resp = client.get('/api/report/morning/recent?type=morning', headers=user_key)
        assert resp.status_code == 200
        _assert_no_private_text(resp.get_json())

    def test_non_admin_weekly_play_review_is_stripped(self, client, db):
        _seed(db, 'weekly', _WEEKLY)
        _login(client, 'member')
        rows = client.get('/api/report/morning/recent?type=weekly').get_json()
        _assert_no_private_text(rows)
        assert 'Next Week Setup' in rows[0]['content']

    def test_admin_session_gets_the_full_report(self, client, db):
        _seed(db, 'morning', _MORNING)
        _login(client, 'admin', user_id=1)
        row = client.get('/api/report/morning/recent?type=morning').get_json()[0]
        assert 'Action Dashboard' in row['content']
        assert row['summary'] == _PRIVATE_SUMMARY
        assert row['audio_script'] == _PRIVATE_SCRIPT

    def test_report_engine_internal_key_gets_the_full_report(self, client, db):
        """The engine's narrative-continuity and weekly-digest fetches."""
        _seed(db, 'morning', _MORNING)
        row = client.get('/api/report/morning/recent?type=morning',
                         headers=_INTERNAL).get_json()[0]
        assert 'Action Dashboard' in row['content']
        assert row['summary'] == _PRIVATE_SUMMARY
        assert row['audio_script'] == _PRIVATE_SCRIPT

    def test_admin_as_general_gets_the_shared_cut(self, client, db):
        _seed(db, 'morning', _MORNING)
        _login(client, 'admin', user_id=1)
        _assert_no_private_text(
            client.get('/api/report/morning/recent?type=morning&as=general').get_json())


class TestReportAudioById:
    @pytest.mark.parametrize('tier', ['member', 'free'])
    def test_session_non_admin_cannot_fetch_narration(self, client, db, audio_dir, tier):
        rid = _seed(db, 'morning', _MORNING)
        _write_mp3(audio_dir, f'report_{rid}.mp3')
        _login(client, tier)
        assert client.get(f'/api/report/{rid}/audio').status_code == 404

    def test_per_user_api_key_cannot_fetch_narration(self, client, db, audio_dir, user_key):
        rid = _seed(db, 'afternoon', _MORNING)
        _write_mp3(audio_dir, f'report_{rid}.mp3')
        assert client.get(f'/api/report/{rid}/audio', headers=user_key).status_code == 404

    def test_admin_session_and_internal_key_get_the_mp3(self, client, db, audio_dir):
        rid = _seed(db, 'morning', _MORNING)
        _write_mp3(audio_dir, f'report_{rid}.mp3')
        resp = client.get(f'/api/report/{rid}/audio', headers=_INTERNAL)
        assert resp.status_code == 200
        assert resp.mimetype == 'audio/mpeg'
        _login(client, 'admin', user_id=1)
        assert client.get(f'/api/report/{rid}/audio').status_code == 200


class TestLatestMorningAudio:
    def test_member_does_not_get_the_operator_narration(
            self, client, db, audio_dir, monkeypatch):
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'true')
        rid = _seed(db, 'morning', _MORNING)
        _write_mp3(audio_dir, f'report_{rid}.mp3')
        _login(client, 'member')
        assert client.get('/api/report/morning/latest/audio').status_code == 404
        assert client.head('/api/report/morning/latest/audio').status_code == 404

    def test_member_still_gets_their_own_personal_narration(
            self, client, db, audio_dir, monkeypatch):
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'true')
        rid = _seed(db, 'morning', _MORNING)
        _write_mp3(audio_dir, f'report_{rid}.mp3')
        _write_mp3(audio_dir, f'report_{rid}_personal_99.mp3', b'ID3personal-99')
        _login(client, 'member', user_id=99)
        resp = client.get('/api/report/morning/latest/audio')
        assert resp.status_code == 200
        assert resp.get_data() == b'ID3personal-99'


class TestBriefXCandidates:
    def test_licensed_candidates_never_reach_the_public_brief(self, client, db):
        import json

        from carrac.reports.routes import _gather_general_report_json

        db.save_report(
            content='## The Story\nNeutral.\n', assets_included=[],
            generation_time_ms=0, report_type='general',
            metadata=json.dumps({'x_candidates': [
                'BTC holds the 60k shelf.', 'DVOL sits at cycle lows.']}))
        assert _gather_general_report_json()['x_candidates'] == ['BTC holds the 60k shelf.']
