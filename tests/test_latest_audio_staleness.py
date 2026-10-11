"""The Listen button must never play a stale narration.

`GET /api/report/morning/latest/audio` resolved `get_latest_report('morning')`
with no freshness bound and no ENABLE_MORNING_REPORT gate would return 200
forever once 'morning' stops generating, as long as its mp3 is still on disk.
The panel's HEAD probe would show a Listen button next to TODAY's text, and
clicking it would play the old narration.

These tests drive the REAL route through the Flask test client against a real
ContentDatabase. The only thing patched is `_audio_dir` — the filesystem
location, which is hardcoded to a repo-relative path and would otherwise write
mp3 fixtures into the working tree. The resolution logic under test is
untouched.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def db(app):  # noqa: ARG001 — app fixture sets DATA_DIR before we bind the singleton
    import carrac.content_db as cdb

    cdb._db_instance = None
    database = cdb.get_content_db()
    yield database
    cdb._db_instance = None


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    """Redirect mp3 storage to a tmp dir.

    _audio_dir() resolves relative to the package, not DATA_DIR, so without
    this the fixtures below would litter the real ./data/audio.
    """
    import carrac.reports.routes as rr

    d = tmp_path / 'audio'
    d.mkdir()
    monkeypatch.setattr(rr, '_audio_dir', lambda: str(d))
    return d


@pytest.fixture
def operator_client(client):
    """The shared narration is operator only; these tests are about freshness."""
    with client.session_transaction() as sess:
        sess['user_id'] = 'admin'
        sess['email'] = 'admin@example.com'
        sess['is_admin'] = True
        sess['tier'] = 'admin'
    return client


def _seed(db, report_type, days_ago, content='report body'):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    conn = db._get_conn()
    cur = conn.execute(
        "INSERT INTO reports "
        "(report_type, content, assets_included, generation_time_ms, summary, "
        "generation_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (report_type, content, '[]', 0, None, when.strftime('%Y-%m-%d'),
         when.strftime('%Y-%m-%d %H:%M:%S')),
    )
    conn.commit()
    return cur.lastrowid


def _write_mp3(audio_dir, report_id, suffix=''):
    path = os.path.join(str(audio_dir), f'report_{report_id}{suffix}.mp3')
    with open(path, 'wb') as fh:
        fh.write(b'ID3fake-mp3-bytes')
    return path


class TestStaleMorningAudioIsNotServed:
    def test_the_prod_bug_46_day_old_audio_is_refused(
            self, operator_client, db, audio_dir, monkeypatch):
        """The core pin: a 'morning' row 46 days old with its mp3 still on
        disk must be refused, not played."""
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'true')
        rid = _seed(db, 'morning', days_ago=46)
        _write_mp3(audio_dir, rid)

        resp = operator_client.get('/api/report/morning/latest/audio')
        assert resp.status_code == 404, \
            'a 46-day-old narration was served as the latest report audio'

    def test_head_probe_also_404s_so_the_button_stays_hidden(
            self, operator_client, db, audio_dir, monkeypatch):
        """The frontend decides whether to render Listen from a HEAD probe
        (static/js/split-layout.js). If HEAD still 200s the button appears and
        the fix is cosmetic only."""
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'true')
        rid = _seed(db, 'morning', days_ago=46)
        _write_mp3(audio_dir, rid)

        assert operator_client.head('/api/report/morning/latest/audio').status_code == 404

    def test_stale_personal_audio_is_refused_too(
            self, auth_client, db, audio_dir, monkeypatch):
        """report_277_personal_1.mp3 exists in production — the operator got the
        June PERSONAL narration. The personal branch must not bypass the
        freshness check."""
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'true')
        rid = _seed(db, 'morning', days_ago=46)
        _write_mp3(audio_dir, rid, suffix='_personal_test-user')

        assert auth_client.get('/api/report/morning/latest/audio').status_code == 404

    def test_disabled_morning_report_serves_no_audio(
            self, operator_client, db, audio_dir, monkeypatch):
        """Prod's actual config. Even a FRESH morning row must not be narrated
        while the feature is off — the text route serves 'general' in this
        mode, so morning audio would contradict the text on screen."""
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'false')
        rid = _seed(db, 'morning', days_ago=0)
        _write_mp3(audio_dir, rid)

        assert operator_client.get('/api/report/morning/latest/audio').status_code == 404

    def test_general_report_does_not_borrow_morning_audio(
            self, operator_client, db, audio_dir, monkeypatch):
        """Today's shape: a fresh 'general' report (no audio ever generated for
        that type) plus the stale 'morning' mp3. The fresh text must NOT be
        paired with the stale narration."""
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'false')
        stale = _seed(db, 'morning', days_ago=46)
        _write_mp3(audio_dir, stale)
        _seed(db, 'general', days_ago=0)

        assert operator_client.get('/api/report/morning/latest/audio').status_code == 404


class TestFreshMorningAudioStillWorks:
    """The staleness gate must not silently kill the feature — if a fresh
    morning report with audio exists, it is still served."""

    def test_todays_audio_is_served(self, operator_client, db, audio_dir, monkeypatch):
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'true')
        rid = _seed(db, 'morning', days_ago=0)
        _write_mp3(audio_dir, rid)

        resp = operator_client.get('/api/report/morning/latest/audio')
        assert resp.status_code == 200
        assert resp.mimetype == 'audio/mpeg'

    def test_yesterdays_audio_is_still_served(
            self, operator_client, db, audio_dir, monkeypatch):
        """A morning report generated yesterday is a normal state (the user
        reads before today's run). The bound must not be so tight it breaks
        the ordinary case."""
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'true')
        rid = _seed(db, 'morning', days_ago=1)
        _write_mp3(audio_dir, rid)

        assert operator_client.get('/api/report/morning/latest/audio').status_code == 200

    def test_personal_audio_preferred_when_fresh(
            self, auth_client, db, audio_dir, monkeypatch):
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'true')
        rid = _seed(db, 'morning', days_ago=0)
        _write_mp3(audio_dir, rid)
        personal = _write_mp3(audio_dir, rid, suffix='_personal_test-user')
        with open(personal, 'wb') as fh:
            fh.write(b'PERSONAL-AUDIO-BYTES')

        resp = auth_client.get('/api/report/morning/latest/audio')
        assert resp.status_code == 200
        assert b'PERSONAL-AUDIO-BYTES' in resp.get_data()

    def test_fresh_report_without_audio_file_404s(
            self, operator_client, db, audio_dir, monkeypatch):  # noqa: ARG002
        """Pre-existing behaviour, preserved: no file means no button."""
        monkeypatch.setenv('ENABLE_MORNING_REPORT', 'true')
        _seed(db, 'morning', days_ago=0)
        assert operator_client.get('/api/report/morning/latest/audio').status_code == 404


class TestSiblingSweep:
    def test_the_unbounded_lookup_is_gone_from_the_audio_route(self):
        """If a future edit drops the bound, the June-audio bug walks
        straight back in."""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / 'carrac' / 'reports' / 'routes.py')
        text = src.read_text()
        start = text.index('def get_latest_report_audio(')
        body = text[start:start + 2600]
        code = '\n'.join(ln for ln in body.split('\n')
                         if not ln.lstrip().startswith('#'))
        assert "get_latest_report(report_type='morning')" not in code
        assert 'max_age_days' in code
        assert 'ENABLE_MORNING_REPORT' in code
