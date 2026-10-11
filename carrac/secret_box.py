"""
Symmetric (Fernet) encryption for secrets stored at rest: Telegram bot
tokens and webhook signing secrets in user prefs, exchange API credentials.

The env var keeps its original name, TELEGRAM_TOKEN_ENCRYPTION_KEY, because
it is deployed configuration; it keys every secret this module encrypts.

Key management:
  - Set TELEGRAM_TOKEN_ENCRYPTION_KEY in env (32-byte url-safe base64 Fernet key).
  - Generate a key with: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  - If the env var is not set, the module logs a startup warning and falls back
    to plaintext storage (backward-compatible with existing deployments).
  - Legacy plaintext tokens (stored before encryption was introduced) are
    returned as-is by decrypt_token(); they will be re-encrypted on next
    user save.

Storage format:
  - Encrypted: "fernet:<ciphertext>"
  - Legacy / missing key: raw token string (no prefix)
"""

import logging
import os

logger = logging.getLogger('carrac.secret_box')

_FERNET_PREFIX = 'fernet:'
_fernet_instance = None
_warned_missing_key = False


def _get_fernet():
    """Lazy-load Fernet instance from env var.

    Returns None if TELEGRAM_TOKEN_ENCRYPTION_KEY is not set, logging a
    warning once per process so operators notice in logs without crashing
    existing deployments.
    """
    global _fernet_instance, _warned_missing_key
    if _fernet_instance is not None:
        return _fernet_instance

    key = os.environ.get('TELEGRAM_TOKEN_ENCRYPTION_KEY', '')
    if not key:
        if not _warned_missing_key:
            logger.warning(
                '[SECURITY] TELEGRAM_TOKEN_ENCRYPTION_KEY is not set — '
                'Telegram bot tokens will be stored in plaintext. '
                'Set this env var to enable at-rest encryption.'
            )
            _warned_missing_key = True
        return None

    try:
        from cryptography.fernet import Fernet
        _fernet_instance = Fernet(key.encode() if isinstance(key, str) else key)
        return _fernet_instance
    except Exception as exc:
        logger.error('[SECURITY] Failed to initialise Fernet from TELEGRAM_TOKEN_ENCRYPTION_KEY: %s', exc)
        return None


class EncryptionUnavailable(RuntimeError):
    """Raised by ``encrypt_token(required=True)`` when no usable key exists.

    Exchange API secrets share this helper with Telegram bot
    tokens. The Telegram path may fall open to plaintext (a bot token is
    the whole blast radius); exchange credentials may not — a missing
    ``TELEGRAM_TOKEN_ENCRYPTION_KEY`` must refuse the write, loudly, the
    way ``SECRET_KEY`` refuses startup in production (carrac/config.py).
    """


def encrypt_token(plain: str, *, required: bool = False, purpose: str = 'Telegram token') -> str:
    """Encrypt a secret for storage.

    ``required=False`` (Telegram bot tokens): if the encryption key is not
    configured, returns the plaintext unchanged so existing deployments keep
    working until the env var is set.

    ``required=True`` (exchange credentials): no key, or a failing key, raises
    ``EncryptionUnavailable`` after an ERROR log. Never returns plaintext.

    Returns:
        "fernet:<ciphertext>" if key is available, else the original string.
    """
    if not plain:
        return plain
    fernet = _get_fernet()
    if fernet is None:
        if required:
            logger.error(
                '[SECURITY] Refusing to store %s: TELEGRAM_TOKEN_ENCRYPTION_KEY '
                'is not set, and this secret must not be written in the clear.',
                purpose,
            )
            raise EncryptionUnavailable(
                f'{purpose} cannot be stored: encryption key is not configured'
            )
        return plain
    try:
        ciphertext = fernet.encrypt(plain.encode('utf-8')).decode('ascii')
        return _FERNET_PREFIX + ciphertext
    except Exception as exc:
        logger.error('[SECURITY] Failed to encrypt %s: %s', purpose, exc)
        if required:
            raise EncryptionUnavailable(f'{purpose} cannot be stored: encryption failed') from exc
        return plain  # Fail open — don't break user save


def decrypt_token(stored: str) -> str:
    """Decrypt a stored Telegram bot token.

    Handles three cases:
      1. Empty / None → return as-is.
      2. Prefixed with "fernet:" → decrypt with Fernet key.
      3. No prefix (legacy plaintext) → return as-is; caller should
         re-encrypt on next user save via encrypt_token().

    Args:
        stored: Value read from user_preferences (may be plaintext or ciphertext).

    Returns:
        Plaintext bot token, or empty string on decryption failure.
    """
    if not stored:
        return stored or ''

    if not stored.startswith(_FERNET_PREFIX):
        # Legacy plaintext — return unchanged so the user's Telegram config
        # keeps working; the token will be encrypted on next settings save.
        return stored

    fernet = _get_fernet()
    if fernet is None:
        # Key disappeared from env while encrypted tokens exist — log loudly
        # but return empty rather than exposing raw ciphertext.
        logger.error(
            '[SECURITY] TELEGRAM_TOKEN_ENCRYPTION_KEY missing but stored token '
            'is encrypted. Cannot decrypt — returning empty string.'
        )
        return ''

    ciphertext = stored[len(_FERNET_PREFIX):]
    try:
        return fernet.decrypt(ciphertext.encode('ascii')).decode('utf-8')
    except Exception as exc:
        logger.error('[SECURITY] Failed to decrypt Telegram token: %s', exc)
        return ''
