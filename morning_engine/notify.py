"""
Notification dispatchers — Telegram + ntfy + TTS upload.

Telegram uses classic-Markdown parse mode; user-supplied strings are
escaped to prevent hijacked formatting. ntfy uses plain text. TTS uploads
the generated MP3 back to the dashboard so ntfy can attach the audio.
"""

from __future__ import annotations

import logging
import os
import re

import requests

from .config import (
    DASHBOARD_API_KEY, DASHBOARD_BASE_URL,
    NTFY_CARRAC_TOPIC,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TTS_ENABLED, TTS_VOICE_ID,
)

# OpenAI TTS fallback. Only activates when OPENAI_API_KEY is set.
# Set in the production .env: OPENAI_API_KEY=sk-...
_OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

log = logging.getLogger('morning_engine')

_API_HEADERS = {"X-Internal-Key": DASHBOARD_API_KEY}

_TG_MARKDOWN_RE = re.compile(r'([_*`\[\]])')


def escape_tg_markdown(text):
    """Escape Telegram's classic-Markdown reserved chars so user-controlled
    labels (scenario names, LLM-generated bullets) can't hijack formatting
    or inject embedded entities. Covers _ * ` [ ] — the only chars the old
    Markdown parser treats as control characters.
    """
    if not text:
        return ''
    return _TG_MARKDOWN_RE.sub(r'\\\1', str(text))


def notify(title, message, audio_url=None, topic=None):
    """Send notification via ntfy and Telegram.

    If audio_url is provided, ntfy will include it as a clickable attachment
    and Telegram will receive it as a voice message.

    `topic` selects the audience. It defaults to the CARRAC (operational)
    topic because almost every caller here is a tripwire or a verifier
    verdict — something is wrong and wants looking at. The handful of
    callers that publish a market digest pass NTFY_MARKET_TOPIC instead;
    those read like a feed and do not belong in an alarm stream.

    Routing everything to NTFY_TOPIC would put every Carrac alarm AND every
    digest in the infrastructure alert stream, hence the separate topics.
    """
    # ntfy (plain text — no escaping needed)
    from carrac.notifications import send_ntfy
    send_ntfy(
        message,
        title=title,
        topic=topic or NTFY_CARRAC_TOPIC,
        priority='default',
        attach=audio_url if audio_url else None,
        filename='morning-briefing.mp3' if audio_url else None,
        timeout=10,
    )

    # Telegram — escape user-supplied message so * / _ / ` / [ ] in scenario
    # labels or LLM bullets can't hijack the Markdown formatting.
    if TELEGRAM_BOT_TOKEN:
        tg_title = escape_tg_markdown(title)
        tg_body = escape_tg_markdown(message)
        try:
            if audio_url:
                # Send audio as voice message
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendAudio",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "audio": audio_url,
                        "caption": f"*{tg_title}*\n{tg_body}",
                        "parse_mode": "Markdown",
                        "title": "Morning Briefing",
                    },
                    timeout=15,
                )
            else:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID,
                          "text": f"*{tg_title}*\n{tg_body}",
                          "parse_mode": "Markdown"},
                    timeout=10,
                )
        except Exception as e:
            from carrac.notifications.redact import describe_exception
            log.warning("Telegram failed: %s", describe_exception(e))


def _tts_openai(text: str) -> bytes:
    """Call OpenAI TTS API and return MP3 bytes.

    Fallback provider when Grok TTS is unavailable.
    Uses model=tts-1, voice=onyx (authoritative male, close to Grok's rex).
    Only activates when OPENAI_API_KEY env var is present; callers should
    check _OPENAI_API_KEY before calling this function.
    """
    resp = requests.post(
        "https://api.openai.com/v1/audio/speech",
        json={"model": "tts-1", "voice": "onyx", "input": text},
        headers={
            "Authorization": f"Bearer {_OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def generate_tts(grok, text, report_id):
    """Generate TTS audio from text and upload to dashboard.

    Tries Grok TTS first; falls back to OpenAI TTS on failure.
    If OPENAI_API_KEY is not set the OpenAI fallback is skipped (no error).
    Fires a Telegram alert when Grok fails so the operator knows the fallback
    activated or that all providers were exhausted.

    Returns the public audio URL if successful, None otherwise.
    """
    if not TTS_ENABLED:
        log.info("TTS disabled, skipping")
        return None

    if not text or len(text.strip()) < 50:
        log.warning("TTS text too short (%d chars), skipping", len(text) if text else 0)
        return None

    audio_data: bytes | None = None
    provider_used = "grok"

    # --- Primary: Grok TTS ---
    try:
        audio_data = grok.tts(text, voice_id=TTS_VOICE_ID)
    except Exception as grok_err:
        log.error("Grok TTS failed: %s", grok_err)

        # Alert operator about the Grok failure.
        if TELEGRAM_BOT_TOKEN:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": (
                            f"⚠️ Grok TTS failed for report {report_id}: {grok_err}. "
                            "Attempting OpenAI fallback."
                            if _OPENAI_API_KEY
                            else f"⚠️ Grok TTS failed for report {report_id}: {grok_err}. "
                            "No OpenAI key set — audio skipped."
                        ),
                        "parse_mode": "Markdown",
                    },
                    timeout=10,
                )
            except Exception as tg_err:
                log.warning("Telegram alert for TTS failure also failed: %s", tg_err)

        # --- Fallback: OpenAI TTS ---
        if _OPENAI_API_KEY:
            try:
                audio_data = _tts_openai(text)
                provider_used = "openai"
                log.info("OpenAI TTS fallback succeeded (%d bytes)", len(audio_data))
            except Exception as oai_err:
                log.error("OpenAI TTS fallback also failed: %s", oai_err)
                if TELEGRAM_BOT_TOKEN:
                    try:
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={
                                "chat_id": TELEGRAM_CHAT_ID,
                                "text": (
                                    f"🔴 All TTS providers failed for report {report_id}. "
                                    f"OpenAI error: {oai_err}. Audio skipped."
                                ),
                                "parse_mode": "Markdown",
                            },
                            timeout=10,
                        )
                    except Exception:
                        pass
        else:
            log.warning("OPENAI_API_KEY not set — TTS fallback skipped")

    if audio_data is None:
        return None

    # Upload to dashboard
    try:
        resp = requests.post(
            f"{DASHBOARD_BASE_URL}/api/report/{report_id}/audio",
            data=audio_data,
            headers={**_API_HEADERS, "Content-Type": "audio/mpeg"},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as upload_err:
        log.error("TTS audio upload failed (%s provider): %s", provider_used, upload_err)
        return None

    from carrac.site_url import site_base_url
    audio_url = f"{site_base_url()}/api/report/{report_id}/audio"
    log.info("TTS audio saved via %s: %d bytes, %s", provider_used, len(audio_data), audio_url)
    return audio_url
