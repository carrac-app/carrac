"""
Security Module - Centralized security configuration and utilities.

This module provides:
- Password validation with industry-standard requirements
- Session security configuration
- Input validation utilities
- Security event logging
- Brute force protection
- Timing-safe authentication helpers
"""

import ipaddress
import os
import re
import time
import secrets
import logging
import hashlib
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Tuple, Optional, Dict, List
from threading import Lock

from carrac.csp import build_csp

logger = logging.getLogger('carrac.security')

# =============================================================================
# Security Constants
# =============================================================================

# Password Policy (NIST-inspired)
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
PASSWORD_REQUIRE_UPPERCASE = False
PASSWORD_REQUIRE_LOWERCASE = False
PASSWORD_REQUIRE_DIGIT = False
PASSWORD_REQUIRE_SPECIAL = True
SPECIAL_CHARACTERS = "!@#$%^&*()_+-=[]{}|;':\",./<>?"

# Common passwords blacklist (top 100 most common)
COMMON_PASSWORDS = {
    'password', 'password123', '123456', '12345678', '123456789',
    'qwerty', 'abc123', 'monkey', '1234567', 'letmein',
    'trustno1', 'dragon', 'baseball', 'iloveyou', 'master',
    'sunshine', 'ashley', 'foobar', 'passw0rd', 'shadow',
    'admin', 'admin123', 'welcome', 'welcome1', 'p@ssw0rd',
    'changeme', 'password1', '1234567890', '000000', '654321',
    'qwerty123', 'michael', 'jennifer', 'hunter', 'soccer',
    'charlie', 'thomas', 'hello', 'donald', 'batman'
}

# Brute Force Protection
MAX_LOGIN_ATTEMPTS = 3
LOCKOUT_DURATION_MINUTES = 15
LOCKOUT_WINDOW_MINUTES = 15
# email-only counter — higher threshold, 1-hour window, sends an
# unlock-link email instead of hard-blocking (attacker from many IPs can't
# permanently lock out the real user; real user gets 1-click recovery).
EMAIL_ONLY_THRESHOLD = 50
EMAIL_ONLY_WINDOW_MINUTES = 60

# Session Security
# Use HTTPS-only cookies when HTTPS is available, configurable via environment
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'true').lower() == 'true'
SESSION_COOKIE_HTTPONLY = True  # No JavaScript access
SESSION_COOKIE_SAMESITE = 'Lax'  # Lax allows PWA standalone mode to retain cookies (Strict breaks iOS PWA)
# Session TTL policy (enforced by `_check_session_idle` in carrac/__init__.py):
#   - Cookie lifetime: 30 days — PWA stays logged in while used regularly.
#   - Absolute cap:    30 days from session creation (_created_at).
#   - Idle cap:        14 days since last activity (_last_active).
# All three must agree; if you change one, bump the others in lock-step.
SESSION_LIFETIME_HOURS = 24 * 30

# The CSRF token must never expire before the session it protects.
# Both the Flask config and the FastAPI middleware read this one value — see
# tests/test_csrf_lifetime.py, which fails if they ever disagree again.
CSRF_TIME_LIMIT_SECONDS = SESSION_LIFETIME_HOURS * 3600
SESSION_ABSOLUTE_MAX_DAYS = 30
SESSION_IDLE_MAX_DAYS = 14
SESSION_REFRESH_EACH_REQUEST = True

# Rate Limiting
RATE_LIMIT_DEFAULT = "200 per day"
RATE_LIMIT_LOGIN = "5 per minute"
RATE_LIMIT_REGISTER = "3 per hour"
RATE_LIMIT_API = "60 per minute"
RATE_LIMIT_ADMIN = "30 per minute"

# Embed API Security
EMBED_API_REQUIRE_TOKEN = True  # Require authentication for embed API

# =============================================================================
# Password Validation
# =============================================================================

def validate_password(password: str) -> Tuple[bool, str]:
    """
    Validate password against security requirements.

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not password:
        return False, "Password is required"

    # Length checks
    if len(password) < PASSWORD_MIN_LENGTH:
        return False, f"Password must be at least {PASSWORD_MIN_LENGTH} characters"

    if len(password) > PASSWORD_MAX_LENGTH:
        return False, f"Password must not exceed {PASSWORD_MAX_LENGTH} characters"

    # Complexity checks
    if PASSWORD_REQUIRE_UPPERCASE and not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"

    if PASSWORD_REQUIRE_LOWERCASE and not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"

    if PASSWORD_REQUIRE_DIGIT and not re.search(r'[0-9]', password):
        return False, "Password must contain at least one digit"

    if PASSWORD_REQUIRE_SPECIAL:
        if not re.search(r'[!@#$%^&*()_+\-=\[\]{}|;\':",./<>?]', password):
            return False, "Password must contain at least one special character (!@#$%^&*...)"

    # Common password check
    password_lower = password.lower()
    if password_lower in COMMON_PASSWORDS:
        return False, "Password is too common. Please choose a more unique password."

    # Check for common patterns
    if re.search(r'(.)\1{3,}', password):  # Same char repeated 4+ times
        return False, "Password contains too many repeated characters"

    if re.search(r'(012|123|234|345|456|567|678|789|890)', password):
        return False, "Password contains sequential numbers"

    if re.search(r'(abc|bcd|cde|def|efg|fgh|ghi|hij|ijk|jkl|klm|lmn|mno|nop|opq|pqr|qrs|rst|stu|tuv|uvw|vwx|wxy|xyz)', password.lower()):
        return False, "Password contains sequential letters"

    # Entropy check (basic)
    unique_chars = len(set(password))
    if unique_chars < len(password) * 0.5:  # Less than 50% unique chars
        return False, "Password has too many repeated characters"

    return True, "Password meets security requirements"


# =============================================================================
# Brute Force Protection
# =============================================================================

# Pluggable callback list so the auth layer can send unlock emails
# without creating a circular import (security.py → carrac.auth).
# Register with: security._email_unlock_callbacks.append(my_fn)
# Signature: fn(email: str) -> None
_email_unlock_callbacks: List = []


def _fire_email_unlock_callbacks(email: str) -> None:
    """Invoke all registered email-unlock callbacks; never raises."""
    for cb in _email_unlock_callbacks:
        try:
            cb(email)
        except Exception as exc:
            logger.warning("[SECURITY] email_unlock_callback failed: %s", exc)


class BruteForceProtection:
    """
    Track and prevent brute force login attempts.

    Uses Redis when available for cross-worker consistency (Gunicorn).
    Falls back to in-memory dict when Redis is unavailable.

    Features:
    - Per-email tracking (not just per-IP)
    - Progressive lockout duration
    - Automatic cleanup of old entries
    - Cross-worker consistency via Redis
    """

    def __init__(self):
        # In-memory fallback
        self._attempts: Dict[str, List[datetime]] = defaultdict(list)
        self._lockouts: Dict[str, datetime] = {}
        self._lock = Lock()
        # Redis connection (lazy init)
        self._redis = None
        self._redis_checked = False
        self._prefix = 'bf:'

    def _get_redis(self):
        """Lazy-init Redis connection. Returns None if unavailable."""
        if self._redis_checked:
            return self._redis
        self._redis_checked = True
        try:
            import redis as redis_mod
            url = os.getenv('REDIS_URL', '')
            if not url:
                raise RuntimeError('REDIS_URL not set')
            r = redis_mod.from_url(url, socket_connect_timeout=1, decode_responses=True)
            r.ping()
            self._redis = r
            logger.info("[SECURITY] Brute force protection using Redis (cross-worker)")
        except Exception:
            logger.info("[SECURITY] Brute force protection using in-memory fallback")
            self._redis = None
        return self._redis

    def _get_email_key(self, email: str) -> str:
        """Generate a key for email-only tracking (no IP)."""
        return hashlib.sha256(f"email:{email.lower()}".encode()).hexdigest()[:32]

    def record_attempt(self, email: str, ip_address: str) -> None:
        """Record a failed login attempt (both email+IP and email-only keys).

        per-(email+IP) counter uses the short lockout path as
        before.  The email-only counter uses a higher threshold and 1-hour
        window; when it fires it sends an unlock-link email to the real owner
        instead of hard-blocking (so an attacker spraying from many IPs
        cannot permanently lock out a legitimate user).
        """
        key = self._get_key(email, ip_address)
        email_key = self._get_email_key(email)
        self._record_attempt_for_key(key, email, ip_address)
        self._record_email_only_attempt(email_key, email)

    def _record_attempt_for_key(self, key: str, email: str, ip_address: str,
                                 threshold: int = MAX_LOGIN_ATTEMPTS) -> None:
        """Record attempt for a single tracking key."""
        r = self._get_redis()

        if r:
            try:
                attempts_key = f'{self._prefix}attempts:{key}'
                lockout_key = f'{self._prefix}lockout:{key}'
                now_ts = time.time()
                # Add attempt with score = timestamp
                r.zadd(attempts_key, {str(now_ts): now_ts})
                # Remove attempts outside the window
                window_start = now_ts - (LOCKOUT_WINDOW_MINUTES * 60)
                r.zremrangebyscore(attempts_key, '-inf', window_start)
                # Set expiry so keys self-clean
                r.expire(attempts_key, LOCKOUT_WINDOW_MINUTES * 60 * 2)

                attempt_count = r.zcard(attempts_key)
                if attempt_count >= threshold:
                    lockout_count = attempt_count // threshold
                    lockout_seconds = LOCKOUT_DURATION_MINUTES * 60 * (2 ** min(lockout_count - 1, 4))
                    r.setex(lockout_key, lockout_seconds, '1')
                    logger.warning(
                        f"[SECURITY] Account locked | Email: {email} | IP: {ip_address} | "
                        f"Duration: {lockout_seconds // 60}min | Attempts: {attempt_count}"
                    )
                return
            except Exception as e:
                logger.warning("[SECURITY] Redis brute-force record_attempt failed, using in-memory fallback: %s", e)

        # In-memory fallback
        now = datetime.now()
        with self._lock:
            self._attempts[key].append(now)
            window_start = now - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
            self._attempts[key] = [
                t for t in self._attempts[key] if t > window_start
            ]
            if len(self._attempts[key]) >= threshold:
                lockout_count = len(self._attempts[key]) // threshold
                lockout_minutes = LOCKOUT_DURATION_MINUTES * (2 ** min(lockout_count, 4))
                self._lockouts[key] = now + timedelta(minutes=lockout_minutes)
                logger.warning(
                    f"[SECURITY] Account locked | Email: {email} | IP: {ip_address} | "
                    f"Duration: {lockout_minutes}min | Attempts: {len(self._attempts[key])}"
                )

    def _record_email_only_attempt(self, email_key: str, email: str) -> None:
        """Track cross-IP attempts for a single email address.

        Uses a longer window (EMAIL_ONLY_WINDOW_MINUTES) and a higher
        threshold (EMAIL_ONLY_THRESHOLD).  When the threshold is reached,
        instead of hard-locking we emit a signal via the module-level
        ``_email_unlock_callbacks`` list so the auth layer can send a
        one-click unlock email.  The counter is NOT turned into a hard
        lockout — the attacker still can't log in (wrong password), and
        the real user can always regain access via the link.
        """
        r = self._get_redis()
        should_notify = False

        if r:
            try:
                attempts_key = f'{self._prefix}attempts:{email_key}'
                notified_key = f'{self._prefix}unlock_notified:{email_key}'
                now_ts = time.time()
                r.zadd(attempts_key, {str(now_ts): now_ts})
                window_start = now_ts - (EMAIL_ONLY_WINDOW_MINUTES * 60)
                r.zremrangebyscore(attempts_key, '-inf', window_start)
                r.expire(attempts_key, EMAIL_ONLY_WINDOW_MINUTES * 60 * 2)
                attempt_count = r.zcard(attempts_key)
                if attempt_count >= EMAIL_ONLY_THRESHOLD:
                    # Only fire the callback once per window (avoid email flood).
                    if not r.exists(notified_key):
                        r.setex(notified_key, EMAIL_ONLY_WINDOW_MINUTES * 60, '1')
                        should_notify = True
                    logger.warning(
                        "[SECURITY] Email-only threshold reached | Email: %s | "
                        "Attempts: %d (no hard block — unlock link %s)",
                        email, attempt_count,
                        "sent" if should_notify else "already sent",
                    )
                return
            except Exception as e:
                logger.warning(
                    "[SECURITY] Redis email-only attempt failed, skipping: %s", e
                )
                return  # Skip in-memory for email-only; best-effort only.

        # In-memory fallback (single-worker only)
        now = datetime.now()
        with self._lock:
            self._attempts[email_key].append(now)
            window_start = now - timedelta(minutes=EMAIL_ONLY_WINDOW_MINUTES)
            self._attempts[email_key] = [
                t for t in self._attempts[email_key] if t > window_start
            ]
            if len(self._attempts[email_key]) >= EMAIL_ONLY_THRESHOLD:
                if email_key not in self._lockouts:
                    # Use lockouts dict as "notified" marker (in-memory only).
                    self._lockouts[email_key] = now + timedelta(minutes=EMAIL_ONLY_WINDOW_MINUTES)
                    should_notify = True
                logger.warning(
                    "[SECURITY] Email-only threshold reached (in-memory) | Email: %s",
                    email,
                )

        if should_notify:
            _fire_email_unlock_callbacks(email)

    def clear_email_only_counter(self, email: str) -> None:
        """Clear the email-only counter after a successful unlock."""
        email_key = self._get_email_key(email)
        r = self._get_redis()
        if r:
            try:
                pipe = r.pipeline()
                pipe.delete(
                    f'{self._prefix}attempts:{email_key}',
                    f'{self._prefix}unlock_notified:{email_key}',
                )
                pipe.execute()
                return
            except Exception:
                pass
        with self._lock:
            self._attempts.pop(email_key, None)
            self._lockouts.pop(email_key, None)

    def is_locked(self, email: str, ip_address: str) -> Tuple[bool, Optional[int]]:
        """Check if the (email, IP) pair is hard-locked.

        The email-only counter does not trigger a hard lockout —
        it only sends an unlock-link email to the real owner.  Only the
        per-(email+IP) short lockout still hard-blocks login attempts.
        """
        key = self._get_key(email, ip_address)
        locked, remaining = self._is_key_locked(key)
        if locked:
            return True, remaining
        return False, None

    def _is_key_locked(self, key: str) -> Tuple[bool, Optional[int]]:
        """Check if a single tracking key is locked."""
        r = self._get_redis()

        if r:
            try:
                lockout_key = f'{self._prefix}lockout:{key}'
                ttl = r.ttl(lockout_key)
                if ttl and ttl > 0:
                    return True, ttl
                return False, None
            except Exception as e:
                logger.warning("[SECURITY] Redis brute-force is_locked check failed, using in-memory fallback: %s", e)

        with self._lock:
            if key not in self._lockouts:
                return False, None
            lockout_until = self._lockouts[key]
            now = datetime.now()
            if now >= lockout_until:
                del self._lockouts[key]
                return False, None
            remaining = int((lockout_until - now).total_seconds())
            return True, remaining

    def clear_attempts(self, email: str, ip_address: str) -> None:
        """Clear attempts after successful login (both email+IP and email-only keys)."""
        ip_key = self._get_key(email, ip_address)
        email_key = self._get_email_key(email)
        r = self._get_redis()

        if r:
            try:
                pipe = r.pipeline()
                # (email+IP) keys
                pipe.delete(
                    f'{self._prefix}attempts:{ip_key}',
                    f'{self._prefix}lockout:{ip_key}',
                )
                # email-only keys (also clear the unlock-notified marker)
                pipe.delete(
                    f'{self._prefix}attempts:{email_key}',
                    f'{self._prefix}lockout:{email_key}',
                    f'{self._prefix}unlock_notified:{email_key}',
                )
                pipe.execute()
                return
            except Exception as e:
                logger.warning("[SECURITY] Redis brute-force clear_attempts failed, using in-memory fallback: %s", e)

        with self._lock:
            for key in (ip_key, email_key):
                self._attempts.pop(key, None)
                self._lockouts.pop(key, None)

    def get_remaining_attempts(self, email: str, ip_address: str) -> int:
        """Get remaining login attempts before lockout."""
        key = self._get_key(email, ip_address)
        r = self._get_redis()

        if r:
            try:
                attempts_key = f'{self._prefix}attempts:{key}'
                current = r.zcard(attempts_key) or 0
                return max(0, MAX_LOGIN_ATTEMPTS - current)
            except Exception as e:
                logger.warning("[SECURITY] Redis brute-force get_remaining failed, using in-memory fallback: %s", e)

        with self._lock:
            current_attempts = len(self._attempts.get(key, []))
            return max(0, MAX_LOGIN_ATTEMPTS - current_attempts)

    def _get_key(self, email: str, ip_address: str) -> str:
        """Generate a unique key for email+IP combination."""
        combined = f"{email.lower()}:{ip_address}"
        return hashlib.sha256(combined.encode()).hexdigest()[:32]

# Global instance
brute_force_protection = BruteForceProtection()


# =============================================================================
# Timing-Safe Authentication Helpers
# =============================================================================

def generate_csp_nonce() -> str:
    """Generate a unique CSP nonce for inline script tags."""
    return secrets.token_urlsafe(32)


def add_timing_noise() -> None:
    """Add random delay to mask timing differences in authentication."""
    # Random delay between 50-150ms
    delay_ms = 50 + secrets.randbelow(100)
    time.sleep(delay_ms / 1000)


# =============================================================================
# Security Event Logging
# =============================================================================

class SecurityEventLogger:
    """Structured security event logging."""

    @staticmethod
    def _mask_email(email: str) -> str:
        """Mask email for GDPR-safe logging: user@domain.com -> u***r@d***n.com"""
        if not email or '@' not in email:
            return '***'
        local, domain = email.rsplit('@', 1)
        masked_local = local[0] + '***' + local[-1] if len(local) > 1 else '***'
        parts = domain.rsplit('.', 1)
        if len(parts) == 2 and len(parts[0]) > 1:
            masked_domain = parts[0][0] + '***' + parts[0][-1] + '.' + parts[1]
        else:
            masked_domain = domain
        return f"{masked_local}@{masked_domain}"

    @staticmethod
    def log_login_success(email: str, ip_address: str, user_agent: str = "") -> None:
        """Log successful login."""
        logger.info(
            f"[SECURITY] LOGIN_SUCCESS | Email: {SecurityEventLogger._mask_email(email)} | IP: {ip_address} | "
            f"User-Agent: {user_agent[:100] if user_agent else 'N/A'}"
        )

    @staticmethod
    def log_login_failure(email: str, ip_address: str, reason: str = "invalid_credentials") -> None:
        """Log failed login attempt."""
        logger.warning(
            f"[SECURITY] LOGIN_FAILED | Email: {SecurityEventLogger._mask_email(email)} | IP: {ip_address} | Reason: {reason}"
        )

    @staticmethod
    def log_registration(email: str, ip_address: str, invite_type: str = "code") -> None:
        """Log new user registration."""
        logger.info(
            f"[SECURITY] REGISTRATION | Email: {SecurityEventLogger._mask_email(email)} | IP: {ip_address} | "
            f"Invite: {invite_type}"
        )

    @staticmethod
    def log_password_reset(email: str, admin_email: str, ip_address: str) -> None:
        """Log password reset by admin."""
        logger.warning(
            f"[SECURITY] PASSWORD_RESET | Target: {SecurityEventLogger._mask_email(email)} | "
            f"Admin: {SecurityEventLogger._mask_email(admin_email)} | IP: {ip_address}"
        )

    @staticmethod
    def log_password_reset_requested(email: str, ip_address: str) -> None:
        """Log self-service password reset request."""
        logger.info(
            f"[SECURITY] PASSWORD_RESET_REQUESTED | Email: {SecurityEventLogger._mask_email(email)} | IP: {ip_address}"
        )

    @staticmethod
    def log_password_reset_completed(email: str, ip_address: str) -> None:
        """Log successful password reset completion."""
        logger.warning(
            f"[SECURITY] PASSWORD_RESET_COMPLETED | Email: {SecurityEventLogger._mask_email(email)} | IP: {ip_address}"
        )

    @staticmethod
    def log_email_verified(email: str, ip_address: str) -> None:
        """Log email verification."""
        logger.info(
            f"[SECURITY] EMAIL_VERIFIED | Email: {SecurityEventLogger._mask_email(email)} | IP: {ip_address}"
        )

    @staticmethod
    def log_account_deleted(email: str, ip_address: str) -> None:
        """Log account deletion."""
        logger.warning(
            f"[SECURITY] ACCOUNT_DELETED | Email: {SecurityEventLogger._mask_email(email)} | IP: {ip_address}"
        )

    @staticmethod
    def log_data_export(email: str, ip_address: str) -> None:
        """Log GDPR data export."""
        logger.info(
            f"[SECURITY] DATA_EXPORT | Email: {SecurityEventLogger._mask_email(email)} | IP: {ip_address}"
        )

security_logger = SecurityEventLogger()


# =============================================================================
# Input Validation
# =============================================================================

def validate_email(email: str) -> Tuple[bool, str]:
    """
    Validate email address format.

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not email:
        return False, "Email is required"

    email = email.strip()

    if len(email) > 254:
        return False, "Email address is too long"

    # Basic email regex (RFC 5322 simplified)
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(pattern, email):
        return False, "Invalid email address format"

    return True, "Email is valid"


def sanitize_string(value: str, max_length: int = 255, allow_html: bool = False) -> str:
    """
    Sanitize a string input.

    Args:
        value: Input string
        max_length: Maximum allowed length
        allow_html: Whether to allow HTML tags

    Returns:
        Sanitized string
    """
    if not value:
        return ""

    # Truncate to max length
    value = value[:max_length]

    # Strip whitespace
    value = value.strip()

    # Remove HTML tags if not allowed
    if not allow_html:
        value = re.sub(r'<[^>]+>', '', value)

    # Remove null bytes and control characters
    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)

    return value


# =============================================================================
# LLM Prompt Injection Protection
# =============================================================================

# Patterns that commonly appear in prompt injection attempts (case-insensitive)
_LLM_INJECTION_PATTERNS = [
    # Direct instruction override attempts
    r'(?i)ignore\s+(all\s+)?previous\s+instructions',
    r'(?i)disregard\s+(all\s+)?previous',
    r'(?i)forget\s+(all\s+)?(your\s+)?previous',
    r'(?i)override\s+(all\s+)?previous',
    r'(?i)new\s+(system\s+)?instructions?\s*:',
    r'(?i)system\s*prompt\s*:',
    # Role impersonation
    r'(?i)you\s+are\s+now\s+(?:a\s+)?(?!analyzing|looking|viewing)',
    r'(?i)act\s+as\s+(?:if\s+you\s+are\s+)?(?:a\s+)?(?:different|new|my)',
    r'(?i)pretend\s+(?:to\s+be|you\s+are)',
    r'(?i)switch\s+to\s+(?:a\s+)?(?:new|different)\s+(?:mode|role|persona)',
    # XML/tag-based injection (chat-template role tags)
    r'</?system(?:-reminder)?(?:\s[^>]*)?>',
    r'</?assistant(?:\s[^>]*)?>',
    r'</?human(?:\s[^>]*)?>',
    r'</?user(?:\s[^>]*)?>',
    r'</?instructions?(?:\s[^>]*)?>',
    r'</?tool_(?:use|result)(?:\s[^>]*)?>',
    # Dangerous command patterns
    r'(?i)(?:run|execute|eval|exec)\s*(?:this|the\s+following)?\s*(?:command|code|script|shell)\s*:',
    r'(?i)(?:curl|wget|bash|sh|python|pip)\s+[\'"]?https?://',
    r'(?i)(?:rm\s+-rf|sudo\s+|chmod\s+|chown\s+)',
    # Override markers
    r'(?i)IMPORTANT\s*:.*(?:override|ignore|disregard|forget)',
    r'(?i)CRITICAL\s*:.*(?:override|ignore|disregard|new\s+task)',
]

_compiled_llm_patterns = [re.compile(p) for p in _LLM_INJECTION_PATTERNS]


def sanitize_for_llm(text: str, max_length: int = 5000, source: str = "unknown") -> str:
    """
    Sanitize text from external sources before including in LLM context.

    Defends against prompt injection by:
    1. Removing known injection patterns (role markers, instruction overrides)
    2. Stripping dangerous control characters
    3. Truncating to prevent context flooding
    4. Logging filtered content for security monitoring

    Args:
        text: Raw text from external source (tweets, transcripts, user labels, etc.)
        max_length: Maximum allowed length (default 5000 chars)
        source: Description of the data source for logging (e.g. "x_feed", "drawing_label")

    Returns:
        Sanitized text safe for LLM consumption
    """
    if not text:
        return ""

    if not isinstance(text, str):
        text = str(text)

    # NFKC normalise to collapse homograph/fullwidth variants before pattern matching
    import unicodedata as _ud
    text = _ud.normalize('NFKC', text)

    # Truncate to max length
    if len(text) > max_length:
        text = text[:max_length] + "\n[...truncated]"

    # Remove null bytes and non-printable control characters (keep \n, \t, \r)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

    # Check and filter injection patterns
    filtered_count = 0
    for pattern in _compiled_llm_patterns:
        matches = pattern.findall(text)
        if matches:
            filtered_count += len(matches)
            text = pattern.sub('[FILTERED]', text)

    if filtered_count > 0:
        logger.warning(
            f"[SECURITY] LLM_INJECTION_FILTERED | Source: {source} | "
            f"Patterns matched: {filtered_count} | "
            f"Text preview: {text[:100]!r}"
        )

    return text


def validate_watchlist_data(data: Dict) -> Tuple[bool, str]:
    """
    Validate watchlist data structure.

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(data, dict):
        return False, "Watchlist data must be an object"

    groups = data.get('groups', [])

    if not isinstance(groups, list):
        return False, "Groups must be an array"

    if len(groups) > 50:
        return False, "Maximum 50 watchlist groups allowed"

    for i, group in enumerate(groups):
        if not isinstance(group, dict):
            return False, f"Group {i} must be an object"

        # Validate id, if present. Legacy bodies (from older clients, or bulk edits
        # from the /add /remove atomic endpoints) may omit 'id' entirely —
        # that stays valid, the backend self-heals a missing id. Uniqueness
        # across groups is enforced by the /groups route, not here (legacy
        # bodies may carry duplicates that _get_groups self-heals).
        if 'id' in group:
            gid = group.get('id')
            if not isinstance(gid, str) or not (1 <= len(gid) <= 64) or not re.match(r'^[A-Za-z0-9._-]+$', gid):
                return False, f"Group {i} has an invalid id"

        # Validate group name
        name = group.get('name', '')
        if not isinstance(name, str) or len(name) > 100:
            return False, f"Group {i} name must be a string (max 100 chars)"

        # Validate symbols
        symbols = group.get('symbols', [])
        if not isinstance(symbols, list):
            return False, f"Group {i} symbols must be an array"

        if len(symbols) > 100:
            return False, f"Group {i} has too many symbols (max 100)"

        for symbol in symbols:
            # Length + charset must accept every symbol the /add endpoint
            # accepts (WatchlistSymbolRequest: <=30 chars; Yahoo-style ids
            # like GC=F contain '=') — a stricter rule here would make
            # grouped saves 400 forever for a user who starred one such
            # symbol via search.
            if not isinstance(symbol, str) or len(symbol) > 30:
                return False, f"Invalid symbol in group {i}"

            if not re.match(r'^[A-Za-z0-9.=_-]+$', symbol):
                return False, f"Invalid symbol format: {symbol}"

    return True, "Watchlist data is valid"


# =============================================================================
# IP Address Validation for Embed API
# =============================================================================



# =============================================================================
# Outbound URL Validation (SSRF defense)
# =============================================================================

# IPv4/IPv6 cloud-metadata addresses that must never be reachable from
# user-supplied URLs. ipaddress.is_link_local already covers 169.254.169.254
# and fd00::*, but this set is kept as a belt-and-braces literal allowlist
# so future provider additions are obvious in code review.
_BLOCKED_METADATA_IPS = {
    '169.254.169.254',  # AWS / GCP / Azure / DO / Oracle metadata
    'fd00:ec2::254',    # AWS IMDSv2 IPv6
}

# Hostnames we never resolve — any of these short-circuits to a reject.
_BLOCKED_HOSTNAMES = {
    'localhost',
    'metadata.google.internal',
    'metadata.goog',
    'metadata',
}

_BLOCKED_HOSTNAME_SUFFIXES = (
    '.local',
    '.internal',
    '.localhost',
)

_MAX_OUTBOUND_URL_LEN = 2048


# NAT64 well-known prefix (RFC 6052): the low 32 bits are an IPv4 address.
_NAT64_PREFIX = ipaddress.ip_network('64:ff9b::/96')


def _is_public_address(ip) -> bool:
    """True only for a globally routable unicast address.

    ``is_global`` is an allowlist, so ranges that are neither private nor
    reserved (the RFC 6598 carrier-grade NAT block, which Tailscale uses,
    or the RFC 6890 protocol-assignment block) still fail. IPv6 forms that carry an IPv4 address
    (IPv4-mapped, NAT64) are judged by the address they carry.
    """
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64_PREFIX:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def validate_outbound_url(url, *, allowed_schemes=('https',), allow_hosts=None):
    """Validate a user-supplied outbound URL for SSRF safety.

    Defends against the class of bug where the only
    check on a webhook URL was ``startswith('https://')``, allowing an
    attacker to point Carrac at ``https://10.0.0.1/admin``,
    ``https://169.254.169.254/...`` (cloud metadata), or
    ``https://errors:8000/`` (an internal Docker DNS name).

    Behaviour:
        * Scheme must be in ``allowed_schemes`` (default: HTTPS only).
        * Hostname is required, NFKC/lowercased, and IDNA-encoded.
        * If ``allow_hosts`` is provided, hostname must match one of
          its entries exactly (after normalisation).
        * Bare IP literals are rejected unconditionally.
        * Hostname is resolved ONCE via ``socket.getaddrinfo``; if any
          resolved address is not globally routable (``is_global``; this
          covers private, loopback, link-local, reserved, carrier-grade
          NAT and IPv4 carried inside IPv6) or is in the blocked metadata
          set, the URL is rejected. Resolving once and pinning the IP defeats DNS-rebind
          attacks that would otherwise return a public IP at validation
          time and a private IP at request time.
        * ``.local`` / ``.internal`` / ``.localhost`` suffixes are
          rejected before any DNS lookup happens.

    Args:
        url: Raw user-supplied URL string.
        allowed_schemes: Tuple of permitted URL schemes.
        allow_hosts: Optional tuple of hostnames the URL must match.

    Returns:
        Tuple of ``(ok, reason_or_pinned_ip)``. On success the second
        element is a string holding the resolved IP literal so the
        caller can issue the request against the IP and pass the
        original hostname in a ``Host`` header to defeat DNS rebind.
        On failure the second element is the human-readable reason.
    """
    import socket
    import unicodedata
    from urllib.parse import urlparse

    if not url or not isinstance(url, str):
        return False, 'URL is required'
    if len(url) > _MAX_OUTBOUND_URL_LEN:
        return False, 'URL too long'

    url = url.strip()

    try:
        parsed = urlparse(url)
    except Exception:
        return False, 'Invalid URL format'

    if parsed.scheme not in allowed_schemes:
        return False, f'Only {"/".join(allowed_schemes).upper()} URLs are allowed'

    raw_hostname = parsed.hostname
    if not raw_hostname:
        return False, 'No hostname in URL'

    # Normalise: NFKC collapses fullwidth/homograph variants, lowercase
    # so the allowlist comparison is case-insensitive, then IDNA-encode
    # so unicode hostnames can't sneak past the literal blocklist.
    try:
        hostname = unicodedata.normalize('NFKC', raw_hostname).lower()
        hostname = hostname.encode('idna').decode('ascii')
    except (UnicodeError, UnicodeEncodeError):
        return False, 'Invalid hostname encoding'

    if hostname in _BLOCKED_HOSTNAMES:
        return False, 'Internal hostname blocked'
    if hostname.endswith(_BLOCKED_HOSTNAME_SUFFIXES):
        return False, 'Internal hostname blocked'

    # Bare IP literals: reject before resolution. Allowing literals would
    # mean a user could just supply 10.0.0.1 directly.
    try:
        ipaddress.ip_address(hostname)
        return False, 'IP addresses not allowed — use a domain name'
    except ValueError:
        pass

    if allow_hosts is not None:
        # Caller-supplied allowlist (e.g. ('ntfy.sh',)). Normalise both
        # sides identically before compare.
        try:
            allowed_set = {
                unicodedata.normalize('NFKC', h).lower().encode('idna').decode('ascii')
                for h in allow_hosts
                if h
            }
        except (UnicodeError, UnicodeEncodeError):
            return False, 'Invalid allowlist'
        if hostname not in allowed_set:
            return False, 'Hostname not in allowlist'

    # Resolve once and pin. Subsequent rebinds are irrelevant because the
    # caller is expected to dial the returned IP literal directly.
    try:
        addrs = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        return False, 'Could not resolve hostname'

    pinned_ip = None
    for family, _, _, _, sockaddr in addrs:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False, 'Resolved address is not a valid IP'
        if str(ip) in _BLOCKED_METADATA_IPS:
            return False, 'URL resolves to a cloud metadata address'
        if not _is_public_address(ip):
            return False, 'URL resolves to a private/internal address'
        if pinned_ip is None:
            pinned_ip = str(ip)

    if pinned_ip is None:
        return False, 'Could not resolve hostname'

    return True, pinned_ip


# =============================================================================
# Flask App Configuration
# =============================================================================

def configure_flask_security(app) -> None:
    """
    Apply security configuration to Flask app.

    Call this after creating the Flask app instance.
    """
    from datetime import timedelta

    # Session security
    app.config['SESSION_COOKIE_SECURE'] = SESSION_COOKIE_SECURE
    app.config['SESSION_COOKIE_HTTPONLY'] = SESSION_COOKIE_HTTPONLY
    app.config['SESSION_COOKIE_SAMESITE'] = SESSION_COOKIE_SAMESITE
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=SESSION_LIFETIME_HOURS)
    app.config['SESSION_REFRESH_EACH_REQUEST'] = SESSION_REFRESH_EACH_REQUEST

    # CSRF settings
    #
    # This was a hardcoded 3600 (1 hour) while
    # PERMANENT_SESSION_LIFETIME is 30 days — the token guarding a session
    # expired 720x sooner than the session itself. The frontend reads the token
    # ONCE from a <meta> tag at page load and has no refresh path, so every
    # write from a tab older than an hour returned 403 while GETs kept working:
    # the app looked alive and silently stopped saving.
    #
    # Measured on a real session: POST /api/indicator-prefs succeeded 20+ times
    # in the first 10 minutes, then returned
    # {"error":"CSRF token missing or invalid"} about 4 hours in. The user
    # retried three times and gave up.
    #
    # DERIVED, not a second constant. The token is signed AND session-bound, so
    # outliving its session buys an attacker nothing; the only real invariant is
    # that it must not expire FIRST. Writing it as an expression means the next
    # person to change the session lifetime cannot reintroduce the drift.
    app.config['WTF_CSRF_TIME_LIMIT'] = CSRF_TIME_LIMIT_SECONDS
    app.config['WTF_CSRF_SSL_STRICT'] = SESSION_COOKIE_SECURE

    # Secret key: Config.SECRET_KEY already handles env → .env → generated fallback.
    # app.config.from_object(config_class) sets it before this function is called.

    logger.info("[SECURITY] Flask security configuration applied")


def add_security_headers(response):
    """
    Add security headers to HTTP response.

    Use as @app.after_request decorator.
    """
    from flask import g, request

    # Per-request nonce from flask.g (set by context processor)
    nonce = getattr(g, 'csp_nonce', '')

    # Headless chart pages need unsafe-eval for Playwright's page.wait_for_function()
    is_headless = request.path.startswith('/chart-headless/')

    # CSP nonce mode: nonce-based script-src, no unsafe-inline (on by
    # default since all inline handlers were migrated to listeners).
    # Set CSP_NONCE_MODE=false in env as a kill-switch if a surface breaks.
    nonce_mode = os.getenv('CSP_NONCE_MODE', 'true').lower() == 'true'
    use_nonce = bool(nonce_mode and nonce)

    response.headers['Content-Security-Policy'] = build_csp(
        nonce if use_nonce else None,
        allow_inline=not use_nonce,
        allow_eval=is_headless,
    )

    # Other security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'

    # Strict Transport Security (HSTS) - only if HTTPS
    if SESSION_COOKIE_SECURE:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains; preload'

    return response


# =============================================================================
