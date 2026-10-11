"""
TTS fallback routing tests.

Tests that generate_tts() correctly falls back from Grok to OpenAI when Grok
fails, fires a Telegram alert on Grok failure, and handles the case where
OPENAI_API_KEY is absent.
"""
from __future__ import annotations

import unittest.mock as mock


import morning_engine.notify as notify_mod


def _make_grok_stub(fail: bool = False):
    """Return a minimal Grok-client stub."""
    stub = mock.MagicMock()
    if fail:
        stub.tts.side_effect = RuntimeError("Grok TTS: upstream 503")
    else:
        stub.tts.return_value = b"MP3_GROK_BYTES"
    return stub


class TestGenerateTTS:
    """Tests for generate_tts() with Grok → OpenAI fallback chain."""

    def test_grok_success_returns_audio_url(self, monkeypatch):
        """Happy path: Grok works, audio is uploaded, URL returned."""
        monkeypatch.setenv("SITE_URL", "https://example.test")
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.setattr(notify_mod, "TTS_ENABLED", True)
        monkeypatch.setattr(notify_mod, "_OPENAI_API_KEY", "")
        monkeypatch.setattr(notify_mod, "TELEGRAM_BOT_TOKEN", "")
        grok = _make_grok_stub(fail=False)

        with mock.patch("morning_engine.notify.requests.post") as mock_post:
            upload_resp = mock.MagicMock()
            upload_resp.raise_for_status.return_value = None
            mock_post.return_value = upload_resp

            url = notify_mod.generate_tts(grok, "A" * 100, report_id=42)

        assert url == "https://example.test/api/report/42/audio"
        grok.tts.assert_called_once()
        # Only the upload call should have been made (no OpenAI, no Telegram).
        assert mock_post.call_count == 1
        call_url = mock_post.call_args[0][0]
        assert "localhost:5000" in call_url or "localhost" in call_url

    def test_grok_failure_triggers_openai_fallback(self, monkeypatch):
        """When Grok raises, OpenAI TTS is called and its bytes are uploaded."""
        monkeypatch.setenv("SITE_URL", "https://example.test")
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.setattr(notify_mod, "TTS_ENABLED", True)
        monkeypatch.setattr(notify_mod, "_OPENAI_API_KEY", "sk-test-key")
        monkeypatch.setattr(notify_mod, "TELEGRAM_BOT_TOKEN", "bot-token")
        monkeypatch.setattr(notify_mod, "TELEGRAM_CHAT_ID", "12345")
        grok = _make_grok_stub(fail=True)

        call_log: list[str] = []

        def fake_post(url, **kwargs):
            call_log.append(url)
            resp = mock.MagicMock()
            resp.raise_for_status.return_value = None
            if "openai.com" in url:
                resp.content = b"MP3_OPENAI_BYTES"
            return resp

        with mock.patch("morning_engine.notify.requests.post", side_effect=fake_post):
            url = notify_mod.generate_tts(grok, "B" * 100, report_id=7)

        assert url == "https://example.test/api/report/7/audio"
        # Telegram alert must have been sent on Grok failure.
        assert any("api.telegram.org" in u for u in call_log), \
            f"Expected Telegram alert call; got: {call_log}"
        # OpenAI TTS must have been called.
        assert any("openai.com" in u for u in call_log), \
            f"Expected OpenAI call; got: {call_log}"
        # Dashboard upload must have been called.
        assert any("localhost" in u for u in call_log), \
            f"Expected upload call; got: {call_log}"

    def test_grok_failure_no_openai_key_returns_none(self, monkeypatch):
        """When Grok fails and OPENAI_API_KEY is absent, generate_tts returns None."""
        monkeypatch.setenv("SITE_URL", "https://example.test")
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.setattr(notify_mod, "TTS_ENABLED", True)
        monkeypatch.setattr(notify_mod, "_OPENAI_API_KEY", "")
        monkeypatch.setattr(notify_mod, "TELEGRAM_BOT_TOKEN", "bot-token")
        monkeypatch.setattr(notify_mod, "TELEGRAM_CHAT_ID", "12345")
        grok = _make_grok_stub(fail=True)

        call_log: list[str] = []

        def fake_post(url, **kwargs):
            call_log.append(url)
            return mock.MagicMock()

        with mock.patch("morning_engine.notify.requests.post", side_effect=fake_post):
            url = notify_mod.generate_tts(grok, "C" * 100, report_id=3)

        assert url is None
        # Telegram alert should still fire.
        assert any("api.telegram.org" in u for u in call_log)
        # OpenAI must NOT have been called.
        assert not any("openai.com" in u for u in call_log), \
            "OpenAI was called despite missing API key"

    def test_all_providers_fail_returns_none(self, monkeypatch):
        """When both Grok and OpenAI fail, generate_tts returns None."""
        monkeypatch.setenv("SITE_URL", "https://example.test")
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.setattr(notify_mod, "TTS_ENABLED", True)
        monkeypatch.setattr(notify_mod, "_OPENAI_API_KEY", "sk-test-key")
        monkeypatch.setattr(notify_mod, "TELEGRAM_BOT_TOKEN", "bot-token")
        monkeypatch.setattr(notify_mod, "TELEGRAM_CHAT_ID", "12345")
        grok = _make_grok_stub(fail=True)

        def fake_post(url, **kwargs):
            if "openai.com" in url:
                raise RuntimeError("OpenAI also down")
            resp = mock.MagicMock()
            resp.raise_for_status.return_value = None
            return resp

        with mock.patch("morning_engine.notify.requests.post", side_effect=fake_post):
            url = notify_mod.generate_tts(grok, "D" * 100, report_id=9)

        assert url is None

    def test_tts_disabled_returns_none_immediately(self, monkeypatch):
        """TTS_ENABLED=False short-circuits before any API call."""
        monkeypatch.setenv("SITE_URL", "https://example.test")
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.setattr(notify_mod, "TTS_ENABLED", False)
        grok = _make_grok_stub(fail=False)

        with mock.patch("morning_engine.notify.requests.post") as mock_post:
            url = notify_mod.generate_tts(grok, "E" * 100, report_id=1)

        assert url is None
        mock_post.assert_not_called()
        grok.tts.assert_not_called()

    def test_short_text_returns_none(self, monkeypatch):
        """Text under 50 chars is skipped (too short for meaningful audio)."""
        monkeypatch.setenv("SITE_URL", "https://example.test")
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.setattr(notify_mod, "TTS_ENABLED", True)
        grok = _make_grok_stub(fail=False)

        with mock.patch("morning_engine.notify.requests.post") as mock_post:
            url = notify_mod.generate_tts(grok, "short", report_id=2)

        assert url is None
        mock_post.assert_not_called()
