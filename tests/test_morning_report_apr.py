"""
Tests for morning report features: audio script DB columns, recent reports API,
personal audio endpoints, preferences whitelist, and dual output parsing.
"""

import os
from unittest.mock import patch


from carrac.content_db import ContentDatabase


# ============================================================
# Audio script DB columns
# ============================================================


class TestAudioScriptDBColumns:
    """Test that audio_script is properly stored and retrieved in reports and personal sections."""

    def _make_db(self, tmp_path):
        return ContentDatabase(str(tmp_path / 'test.db'))

    def test_save_report_with_audio_script(self, tmp_path):
        db = self._make_db(tmp_path)
        report_id = db.save_report(
            content='Test report content',
            assets_included=['BTC', 'ETH'],
            generation_time_ms=1500,
            report_type='morning',
            audio_script='This is the spoken briefing.',
        )
        assert report_id is not None
        report = db.get_report_by_id(report_id)
        assert report['audio_script'] == 'This is the spoken briefing.'

    def test_save_report_without_audio_script(self, tmp_path):
        db = self._make_db(tmp_path)
        report_id = db.save_report(
            content='No audio report',
            assets_included=['BTC'],
            generation_time_ms=500,
            report_type='morning',
        )
        report = db.get_report_by_id(report_id)
        assert report['audio_script'] is None

    def test_update_report_audio_script(self, tmp_path):
        db = self._make_db(tmp_path)
        report_id = db.save_report(
            content='Report to update',
            assets_included=['BTC'],
            generation_time_ms=1000,
            report_type='morning',
        )
        assert db.update_report_audio_script(report_id, 'Updated spoken script')
        report = db.get_report_by_id(report_id)
        assert report['audio_script'] == 'Updated spoken script'

    def test_update_audio_script_overwrites_existing(self, tmp_path):
        db = self._make_db(tmp_path)
        report_id = db.save_report(
            content='Report',
            assets_included=['BTC'],
            generation_time_ms=800,
            report_type='morning',
            audio_script='Original script',
        )
        db.update_report_audio_script(report_id, 'Replaced script')
        report = db.get_report_by_id(report_id)
        assert report['audio_script'] == 'Replaced script'

    def test_get_latest_report_returns_audio_script(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save_report(
            content='Latest morning report',
            assets_included=['BTC', 'SOL'],
            generation_time_ms=2000,
            report_type='morning',
            audio_script='Good morning, here is your briefing.',
        )
        latest = db.get_latest_report(report_type='morning')
        assert latest is not None
        assert latest['audio_script'] == 'Good morning, here is your briefing.'

    def test_save_personal_section_with_audio_script(self, tmp_path):
        db = self._make_db(tmp_path)
        report_id = db.save_report(
            content='Base report',
            assets_included=['BTC'],
            generation_time_ms=500,
            report_type='morning',
        )
        section_id = db.save_personal_section(
            report_id=report_id,
            user_id=1,
            content='Your personal analysis',
            audio_script='Personalized briefing for you.',
        )
        assert section_id is not None

    def test_get_personal_section_returns_audio_script(self, tmp_path):
        db = self._make_db(tmp_path)
        report_id = db.save_report(
            content='Base report',
            assets_included=['ETH'],
            generation_time_ms=600,
            report_type='morning',
        )
        db.save_personal_section(
            report_id=report_id,
            user_id=42,
            content='Personal content',
            audio_script='Your personal audio briefing.',
        )
        section = db.get_personal_section(report_id, user_id=42)
        assert section is not None
        assert section['audio_script'] == 'Your personal audio briefing.'

    def test_get_personal_section_without_audio_script(self, tmp_path):
        db = self._make_db(tmp_path)
        report_id = db.save_report(
            content='Base',
            assets_included=['BTC'],
            generation_time_ms=300,
            report_type='morning',
        )
        db.save_personal_section(
            report_id=report_id,
            user_id=7,
            content='Just text, no audio',
        )
        section = db.get_personal_section(report_id, user_id=7)
        assert section is not None
        assert section['audio_script'] is None


# ============================================================
# Recent reports API returns audio_script
# ============================================================


class TestRecentReportsAPI:
    """Test that /api/report/morning/recent includes audio_script."""

    def test_recent_reports_include_audio_script(self, app):
        client = app.test_client()

        # Seed a morning report with audio_script via the DB
        with app.app_context():
            from carrac.content_db import get_content_db
            db = get_content_db()
            db.save_report(
                content='Morning report with audio',
                assets_included=['BTC', 'ETH'],
                generation_time_ms=1200,
                report_type='morning',
                audio_script='Key points for today: BTC above 100k.',
            )

        # Authenticate with API key
        resp = client.get(
            '/api/report/morning/recent?limit=5',
            headers={'X-Internal-Key': 'test-api-key-for-testing'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert 'audio_script' in data[0]
        assert data[0]['audio_script'] == 'Key points for today: BTC above 100k.'

    def test_recent_reports_audio_script_empty_when_not_set(self, app):
        client = app.test_client()

        with app.app_context():
            from carrac.content_db import get_content_db
            db = get_content_db()
            db.save_report(
                content='No audio report',
                assets_included=['SOL'],
                generation_time_ms=900,
                report_type='morning',
            )

        resp = client.get(
            '/api/report/morning/recent?limit=5',
            headers={'X-Internal-Key': 'test-api-key-for-testing'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1
        # When not set, audio_script should be empty string or None
        assert data[0].get('audio_script') in (None, '', 'None')


# ============================================================
# Personal audio endpoints
# ============================================================


class TestPersonalAudioEndpoints:
    """Test personal TTS audio save/load and latest/audio preference."""

    def test_save_personal_audio(self, app, tmp_path):
        client = app.test_client()

        # Seed a report so the endpoint has something to attach to
        with app.app_context():
            from carrac.content_db import get_content_db
            db = get_content_db()
            report_id = db.save_report(
                content='Audio test report',
                assets_included=['BTC'],
                generation_time_ms=500,
                report_type='morning',
            )

        audio_dir = str(tmp_path / 'audio')
        with patch('carrac.reports.routes._audio_dir', return_value=audio_dir):
            os.makedirs(audio_dir, exist_ok=True)
            # POST binary MP3 data (fake but > 100 bytes)
            fake_mp3 = b'\xff\xfb\x90\x00' + b'\x00' * 500
            resp = client.post(
                f'/api/report/{report_id}/personal/1/audio',
                data=fake_mp3,
                content_type='application/octet-stream',
                headers={'X-Internal-Key': 'test-api-key-for-testing'},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['success'] is True
            assert data['size_bytes'] == len(fake_mp3)

            # Verify file was written
            expected_path = os.path.join(audio_dir, f'report_{report_id}_personal_1.mp3')
            assert os.path.exists(expected_path)

    def test_get_personal_audio_404_when_missing(self, app, tmp_path):
        client = app.test_client()

        with app.app_context():
            from carrac.content_db import get_content_db
            db = get_content_db()
            report_id = db.save_report(
                content='Report without audio',
                assets_included=['BTC'],
                generation_time_ms=500,
                report_type='morning',
            )

        audio_dir = str(tmp_path / 'audio_empty')
        os.makedirs(audio_dir, exist_ok=True)
        with patch('carrac.reports.routes._audio_dir', return_value=audio_dir):
            resp = client.get(
                f'/api/report/{report_id}/personal/99/audio',
                headers={'X-Internal-Key': 'test-api-key-for-testing'},
            )
            assert resp.status_code == 404
            data = resp.get_json()
            assert 'no personal audio' in data['error']

    def test_latest_audio_prefers_personal(self, app, tmp_path):
        """When a logged-in user has personal audio, /latest/audio should serve it."""
        client = app.test_client()

        with app.app_context():
            from carrac.content_db import get_content_db
            db = get_content_db()
            report_id = db.save_report(
                content='Morning for audio test',
                assets_included=['BTC'],
                generation_time_ms=700,
                report_type='morning',
            )

        audio_dir = str(tmp_path / 'audio_pref')
        os.makedirs(audio_dir, exist_ok=True)

        # Create both public and personal audio files
        public_path = os.path.join(audio_dir, f'report_{report_id}.mp3')
        personal_path = os.path.join(audio_dir, f'report_{report_id}_personal_test-user.mp3')
        with open(public_path, 'wb') as f:
            f.write(b'PUBLIC_AUDIO_CONTENT_' + b'\x00' * 100)
        with open(personal_path, 'wb') as f:
            f.write(b'PERSONAL_AUDIO_CONTENT_' + b'\x00' * 100)

        with patch('carrac.reports.routes._audio_dir', return_value=audio_dir):
            # Log in as user with session
            with client.session_transaction() as sess:
                sess['user_id'] = 'test-user'
                sess['email'] = 'test@example.com'
                sess['tier'] = 'member'

            resp = client.get('/api/report/morning/latest/audio')
            assert resp.status_code == 200
            assert resp.content_type == 'audio/mpeg'
            # Should get personal audio, not public
            assert b'PERSONAL_AUDIO_CONTENT_' in resp.data

    def test_latest_audio_falls_back_to_public(self, app, tmp_path):
        """With no personal audio, the operator gets the shared narration."""
        client = app.test_client()

        with app.app_context():
            from carrac.content_db import get_content_db
            db = get_content_db()
            report_id = db.save_report(
                content='Morning for fallback test',
                assets_included=['ETH'],
                generation_time_ms=600,
                report_type='morning',
            )

        audio_dir = str(tmp_path / 'audio_fallback')
        os.makedirs(audio_dir, exist_ok=True)

        # Only create public audio (no personal)
        public_path = os.path.join(audio_dir, f'report_{report_id}.mp3')
        with open(public_path, 'wb') as f:
            f.write(b'PUBLIC_ONLY_AUDIO_' + b'\x00' * 100)

        with patch('carrac.reports.routes._audio_dir', return_value=audio_dir):
            with client.session_transaction() as sess:
                sess['user_id'] = 'admin'
                sess['email'] = 'admin@example.com'
                sess['tier'] = 'admin'

            resp = client.get('/api/report/morning/latest/audio')
            assert resp.status_code == 200
            assert b'PUBLIC_ONLY_AUDIO_' in resp.data


# ============================================================
# Dual output parsing (---SPOKEN_SCRIPT--- separator)
# ============================================================


class TestDualOutputParsing:
    """Test the separator parsing logic used by the morning engine."""

    SEPARATOR = "---SPOKEN_SCRIPT---"

    @staticmethod
    def _parse_dual_output(raw_output: str):
        """Reproduce the parsing logic from morning_engine/engine.py."""
        separator = "---SPOKEN_SCRIPT---"
        if separator in raw_output:
            parts = raw_output.split(separator, 1)
            display_script = parts[0].strip()
            spoken_script = parts[1].strip()
            # Clean up section headers from the display part
            for prefix in ("=== PART 1:", "=== PART 2:", "KEY POINTS"):
                if display_script.upper().startswith(prefix.upper()):
                    display_script = display_script[len(prefix):].strip()
                    break
            for prefix in ("=== PART 2:", "SPOKEN SCRIPT"):
                if spoken_script.upper().startswith(prefix.upper()):
                    spoken_script = spoken_script[len(prefix):].strip()
                    break
            return display_script, spoken_script
        else:
            return raw_output, raw_output

    def test_separator_splits_into_display_and_spoken(self):
        raw = (
            "KEY POINTS\n"
            "- BTC up 5% overnight\n"
            "- ETH holding support\n"
            "---SPOKEN_SCRIPT---\n"
            "Good morning. Bitcoin surged five percent overnight while "
            "Ethereum holds key support levels."
        )
        display, spoken = self._parse_dual_output(raw)
        assert '- BTC up 5%' in display
        assert '- ETH holding support' in display
        assert 'Good morning' in spoken
        assert 'Bitcoin surged' in spoken
        # Separator should not appear in either output
        assert self.SEPARATOR not in display
        assert self.SEPARATOR not in spoken

    def test_key_points_header_stripped_from_display(self):
        raw = (
            "KEY POINTS\n"
            "- Point one\n"
            "---SPOKEN_SCRIPT---\n"
            "Spoken version here."
        )
        display, spoken = self._parse_dual_output(raw)
        assert not display.startswith('KEY POINTS')
        assert display.startswith('- Point one')

    def test_spoken_script_header_stripped(self):
        raw = (
            "Display content\n"
            "---SPOKEN_SCRIPT---\n"
            "SPOKEN SCRIPT\n"
            "Here is the actual spoken content."
        )
        display, spoken = self._parse_dual_output(raw)
        assert not spoken.startswith('SPOKEN SCRIPT')
        assert spoken.startswith('Here is the actual spoken content.')

    def test_fallback_when_separator_missing(self):
        raw = "This is a complete report with no separator. Markets are mixed today."
        display, spoken = self._parse_dual_output(raw)
        assert display == raw
        assert spoken == raw
        assert display == spoken

    def test_empty_spoken_section(self):
        raw = "Display only content\n---SPOKEN_SCRIPT---\n"
        display, spoken = self._parse_dual_output(raw)
        assert display == 'Display only content'
        assert spoken == ''

    def test_multiple_separators_splits_on_first(self):
        raw = (
            "Part A\n"
            "---SPOKEN_SCRIPT---\n"
            "Spoken part\n"
            "---SPOKEN_SCRIPT---\n"
            "This is still part of spoken"
        )
        display, spoken = self._parse_dual_output(raw)
        assert display == 'Part A'
        assert '---SPOKEN_SCRIPT---' in spoken  # second separator is part of spoken text
        assert 'This is still part of spoken' in spoken

    def test_part_1_header_stripped(self):
        raw = (
            "=== PART 1: Key Takeaways\n"
            "- Market up\n"
            "---SPOKEN_SCRIPT---\n"
            "Spoken content"
        )
        display, spoken = self._parse_dual_output(raw)
        assert not display.startswith('=== PART 1:')
        assert 'Key Takeaways' in display

    def test_part_2_header_stripped_from_spoken(self):
        raw = (
            "Display\n"
            "---SPOKEN_SCRIPT---\n"
            "=== PART 2: Audio Briefing\n"
            "Your morning briefing content."
        )
        display, spoken = self._parse_dual_output(raw)
        assert not spoken.startswith('=== PART 2:')
        assert 'Audio Briefing' in spoken


# ============================================================
# Report write endpoints reject session-auth fallback
# ============================================================


class TestReportWriteEndpointsRequireApiKey:
    """/api/report/morning/<id>/summary|metadata|audio-script are
    engine-only endpoints. A logged-in non-admin user must not pass through
    on a session alone (`if 'user_id' not in session: return 401` would give
    session callers a free pass): only the shared API key authorizes
    these writes; session auth is rejected.
    """

    def test_save_summary_rejects_session_auth(self, auth_client):
        resp = auth_client.put('/api/report/morning/1/summary',
                               json={'summary': 'NOT AUTHORIZED'})
        assert resp.status_code == 401
        assert b'unauthorized' in resp.data

    def test_save_metadata_rejects_session_auth(self, auth_client):
        resp = auth_client.put('/api/report/morning/1/metadata',
                               json={'foo': 'bar'})
        assert resp.status_code == 401
        assert b'unauthorized' in resp.data

    def test_save_audio_script_rejects_session_auth(self, auth_client):
        resp = auth_client.put('/api/report/morning/1/audio-script',
                               json={'audio_script': 'NOT AUTHORIZED'})
        assert resp.status_code == 401
        assert b'unauthorized' in resp.data

    def test_save_summary_with_api_key_accepted(self, client):
        """Sanity: the API-key path still works (engine will use this)."""
        resp = client.put(
            '/api/report/morning/999999/summary',
            json={'summary': 'engine-written'},
            headers={'X-Internal-Key': os.environ.get('SCREENSHOT_API_KEY',
                                                 'test-api-key-for-testing')},
        )
        # 500 (no such report_id) or 200 — not 401. That's the contract.
        assert resp.status_code != 401
