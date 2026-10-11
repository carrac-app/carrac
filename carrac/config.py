"""
Application configuration.

Reads from environment variables (set via .env file or Docker env).
Replaces all manual .env file parsing scattered through the codebase.
"""

import os
import secrets


def _read_env_file():
    """Read .env file and set missing env vars. Called once at import time.

    Set CARRAC_SKIP_DOTENV=1 to skip the file entirely (the test suite does
    this so a developer's local .env can never change test outcomes).
    """
    if os.environ.get('CARRAC_SKIP_DOTENV', '').lower() in ('1', 'true', 'yes'):
        return
    try:
        with open('.env', 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                # Don't override existing env vars
                if key and not os.environ.get(key):
                    os.environ[key] = value
    except FileNotFoundError:
        pass


# Load .env file on import (before any config reads)
_read_env_file()


# Every SECRET_KEY value that ships in a template or doc. These are publicly
# known, so a deployment running with one has forgeable sessions and CSRF
# tokens. Keep in sync with .env.example (tests/test_oss_config_defaults.py
# checks that it is).
_PLACEHOLDER_SECRET_KEYS = frozenset({
    'generate-me-with-the-command-above',
    'change-me',
    'changeme',
    'change-me-secret-key',
    'replace-me',
    'secret',
    'secret-key',
    'dev-secret-key',
})

# Reserved / example domains: an ADMIN_EMAIL here is a template value.
_PLACEHOLDER_EMAIL_DOMAINS = frozenset({'example.com', 'example.org', 'example.net'})


class Config:
    """Base configuration."""

    # Flask
    _raw_key = os.getenv('SECRET_KEY', '')
    # Validity = non-placeholder + length + a minimal entropy heuristic. The
    # last guard rejects low-entropy keys (e.g. 'aaaaaaaaaaaaaaaa' or a padded
    # dictionary word) that a length-only check would pass — SECRET_KEY signs
    # session cookies AND CSRF tokens, so a weak value is forgeable. A real
    # generated key (e.g. secrets.token_hex(32) → 16 distinct hex chars) clears
    # the >=8-distinct-character floor with room to spare.
    _key_valid = bool(
        _raw_key
        and 'your-secret' not in _raw_key.lower()
        and _raw_key.strip().lower() not in _PLACEHOLDER_SECRET_KEYS
        and len(_raw_key) >= 16
        and len(set(_raw_key)) >= 8
    )
    _is_prod = bool(os.getenv('DATABASE_URL'))  # production always has DATABASE_URL (PostgreSQL)
    if _is_prod and not _key_valid:
        raise RuntimeError(
            "SECRET_KEY must be set in production to a strong, high-entropy value "
            "(>=16 chars, >=8 distinct characters, not the placeholder)"
        )
    SECRET_KEY = _raw_key if _key_valid else secrets.token_hex(32)
    # ADMIN_EMAIL confers admin on whoever registers that address. The
    # .env.example placeholder (and any address on an RFC 2606 reserved
    # domain, which nobody can own) would hand admin to the first stranger
    # who registers it, so production refuses to start with one.
    _admin_email = os.getenv('ADMIN_EMAIL', '').strip().lower()
    if _is_prod and _admin_email and _admin_email.rsplit('@', 1)[-1] in _PLACEHOLDER_EMAIL_DOMAINS:
        raise RuntimeError(
            "ADMIN_EMAIL is still a placeholder (%s); set it to your own address "
            "or leave it unset" % _admin_email
        )
    DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'

    # Session
    SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'true').lower() == 'true'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'  # Lax allows PWA standalone to retain cookies
    SESSION_PERMANENT = True  # Cookie gets max_age instead of being session-only
    PERMANENT_SESSION_LIFETIME = 86400 * 30  # 30 days

    # Auth — there is deliberately no ADMIN_USER / ADMIN_PASSWORD here.
    # Both were defined for years and read by no auth path; the
    # variable that actually confers admin is ADMIN_EMAIL, matched against the
    # registered address in user_db.py. Carrying dead settings in the example
    # env sent newcomers down a login route that could not work.

    # API Keys
    SCREENSHOT_API_KEY = os.getenv('SCREENSHOT_API_KEY', '')

    # Screenshot service (dedicated Playwright container)
    SCREENSHOT_SERVICE_URL = os.getenv('SCREENSHOT_SERVICE_URL', 'http://screenshot-service:5001')
    SCREENSHOT_SERVICE_KEY = os.getenv('SCREENSHOT_SERVICE_KEY', '')

    # External APIs
    COINGLASS_API_KEY = os.getenv('COINGLASS_API_KEY', '')

    # Push Notifications (VAPID)
    VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '')
    VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
    VAPID_SUBJECT = os.getenv('VAPID_SUBJECT', 'mailto:admin@example.com')

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

    # ntfy — one topic per audience, so a stream stays readable.
    #   NTFY_CARRAC_TOPIC  operational: is Carrac broken (alarms, guards, deploys)
    #   NTFY_MARKET_TOPIC  price action: the report digests, read like a feed
    #   NTFY_TOPIC         general infrastructure alerts — NOT for Carrac. Kept
    #                      for infrastructure senders outside the app.
    # No cross-fallback on purpose: an unset topic must be loud, not quietly
    # rerouted back into the infrastructure stream the split exists to empty.
    NTFY_TOPIC = os.getenv('NTFY_TOPIC', '')
    NTFY_CARRAC_TOPIC = os.getenv('NTFY_CARRAC_TOPIC', '')
    NTFY_MARKET_TOPIC = os.getenv('NTFY_MARKET_TOPIC', '')

    # Webhook
    # Env-supplied, empty when unset like TELEGRAM_* and NTFY_TOPIC above.
    # ⚠ Nothing reads this today — `rg WEBHOOK_BASE_URL` finds only this line.
    # Left in place rather than deleted because removing config is a different
    # decision from scrubbing an address out of it; flagged so it is not
    # mistaken for a live setting.
    WEBHOOK_BASE_URL = os.getenv('WEBHOOK_BASE_URL', '')

    # Site URL (used for payment callbacks, email links, etc.)
    # Defaults to the local dev address; set it to your public origin in production.
    SITE_URL = os.getenv('SITE_URL', 'http://localhost:5002')

    # Community invite link shown in the app menu and welcome email. Empty
    # (the default) hides the link everywhere.
    DISCORD_INVITE_URL = os.getenv('DISCORD_INVITE_URL', '')

    # PostHog Analytics
    POSTHOG_API_KEY = os.getenv('POSTHOG_API_KEY', '')
    POSTHOG_HOST = os.getenv('POSTHOG_HOST', 'https://eu.i.posthog.com')

    # EspoCRM Integration
    ESPOCRM_URL = os.getenv('ESPOCRM_URL', '')
    ESPOCRM_API_KEY = os.getenv('ESPOCRM_API_KEY', '')

    # Cloudflare Turnstile (bot-wall on the open-signup register form).
    # Site key is public (embedded in HTML); secret key is server-side only.
    # When either is unset the register flow skips the check (no-op).
    TURNSTILE_SITE_KEY = os.getenv('TURNSTILE_SITE_KEY', '')
    TURNSTILE_SECRET_KEY = os.getenv('TURNSTILE_SECRET_KEY', '')

    # AI LLM Provider
    # AI_PROVIDER: 'anthropic' or 'openai' (auto-detected from model name if omitted)
    AI_PROVIDER = os.getenv('AI_PROVIDER', '')

    # OpenAI-compatible providers (xAI, Together.ai, etc.)
    AI_API_KEY = os.getenv('AI_API_KEY', '') or os.getenv('TOGETHER_API_KEY', '') or os.getenv('XAI_API_KEY', '')
    # Single source for the runtime default (carrac/llm/openai_compat.py reads
    # Config.AI_BASE_URL instead of re-declaring its own default) — same
    # precedent as the POSTHOG_HOST fix in carrac/integrations.py::_init_posthog_config,
    # which drifted from this file's default before being unified.
    AI_BASE_URL = os.getenv('AI_BASE_URL', 'https://api.x.ai/v1/chat/completions')
    AI_MODEL = os.getenv('AI_MODEL', 'moonshotai/Kimi-K2.5')

    # Anthropic-specific (used when AI_PROVIDER=anthropic or model starts with claude-)
    ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')

    # Legacy compat
    TOGETHER_API_KEY = AI_API_KEY
    TOGETHER_MODEL = AI_MODEL

    # AI per-1M-token rates charged to users (USD). Defaults are 16.0 input /
    # 80.0 output; override with AI_INPUT_RATE / AI_OUTPUT_RATE to match your provider.
    AI_INPUT_RATE_PER_M = float(os.getenv('AI_INPUT_RATE', '16.0'))
    AI_OUTPUT_RATE_PER_M = float(os.getenv('AI_OUTPUT_RATE', '80.0'))
    AI_CONSERVATIVE_ESTIMATE_CENTS = int(os.getenv('AI_ESTIMATE_CENTS', '25'))

    # Fill the gaps for helpers that read these via current_app.config
    # without a corresponding class attribute. Defaults
    # mirror the inline literals in the helpers; env can override per-deployment.
    AI_GLOBAL_DAILY_USD_CAP = float(os.getenv('AI_GLOBAL_DAILY_USD_CAP', '30'))
    AI_JOURNAL_REVIEW_ESTIMATE = int(os.getenv('AI_JOURNAL_REVIEW_ESTIMATE', '25'))
    AI_SESSION_SUMMARY_ESTIMATE = int(os.getenv('AI_SESSION_SUMMARY_ESTIMATE', '25'))
    AI_WEEKLY_REVIEW_ESTIMATE = int(os.getenv('AI_WEEKLY_REVIEW_ESTIMATE', '35'))
    AI_NL_QUERY_ESTIMATE = int(os.getenv('AI_NL_QUERY_ESTIMATE', '20'))
    PORT = int(os.getenv('PORT', '5000'))

    # Credit markup: multiplier applied to raw API cost when charging users.
    # Default 3.0. Set to 1.0 to charge at cost.
    CREDIT_MARKUP = float(os.getenv('CREDIT_MARKUP', '3.0'))

    # Provider cost rates, used for cost reporting (not for user billing)
    # Anthropic Opus: $5.00/$25.00  |  xAI Grok: $0.20/$0.50
    AI_PROVIDER_INPUT_RATE = float(os.getenv('AI_PROVIDER_INPUT_RATE', '5.0'))
    AI_PROVIDER_OUTPUT_RATE = float(os.getenv('AI_PROVIDER_OUTPUT_RATE', '25.0'))
    MORNING_REPORT_COST_CENTS = 30  # charged once per day on first view

    # Paddle Billing (Merchant of Record)
    PADDLE_API_KEY = os.getenv('PADDLE_API_KEY', '')
    PADDLE_ENVIRONMENT = os.getenv('PADDLE_ENVIRONMENT', 'sandbox')  # 'sandbox' or 'production'
    PADDLE_WEBHOOK_SECRET = os.getenv('PADDLE_WEBHOOK_SECRET', '')
    PADDLE_CLIENT_TOKEN = os.getenv('PADDLE_CLIENT_TOKEN', '')  # for paddle.js checkout overlay

    # Price ID → tier/credits mapping
    PADDLE_PRICE_MEMBER = os.getenv('PADDLE_PRICE_MEMBER', '')
    PADDLE_PRICE_PREMIUM = os.getenv('PADDLE_PRICE_PREMIUM', '')
    PADDLE_PRICE_TOPUP_25 = os.getenv('PADDLE_PRICE_TOPUP_25', '')
    PADDLE_PRICE_TOPUP_50 = os.getenv('PADDLE_PRICE_TOPUP_50', '')
    PADDLE_PRICE_TOPUP_100 = os.getenv('PADDLE_PRICE_TOPUP_100', '')

    @staticmethod
    def paddle_configured():
        return bool(Config.PADDLE_API_KEY)

    @staticmethod
    def paddle_price_to_tier():
        """Map Paddle price IDs to Carrac tiers."""
        # Unset (empty) IDs are dropped: '' must never resolve to a tier.
        return {k: v for k, v in {
            Config.PADDLE_PRICE_MEMBER: 'member',
            Config.PADDLE_PRICE_PREMIUM: 'premium',
        }.items() if k}

    @staticmethod
    def paddle_price_to_credits():
        """Map Paddle price IDs to credit amounts (cents)."""
        return {k: v for k, v in {
            Config.PADDLE_PRICE_TOPUP_25: 2500,
            Config.PADDLE_PRICE_TOPUP_50: 5000,
            Config.PADDLE_PRICE_TOPUP_100: 10000,
        }.items() if k}

    # Email (Brevo SMTP)
    BREVO_SMTP_HOST = os.getenv('BREVO_SMTP_HOST', 'smtp-relay.brevo.com')
    BREVO_SMTP_PORT = int(os.getenv('BREVO_SMTP_PORT', '587'))
    BREVO_SMTP_USER = os.getenv('BREVO_SMTP_USER', '')
    BREVO_SMTP_PASS = os.getenv('BREVO_SMTP_PASS', '') or os.getenv('BREVO_SMTP_PASSWORD', '')
    BREVO_FROM_EMAIL = os.getenv('BREVO_FROM_EMAIL', '')

    # Database
    DATABASE_URL = os.getenv('DATABASE_URL', '')
    DB_TYPE = os.getenv('DB_TYPE', 'postgresql' if os.getenv('DATABASE_URL') else 'sqlite')

    # Redis (optional — falls back to in-memory/file storage if unavailable)
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    REDIS_SESSION_URL = os.getenv('REDIS_SESSION_URL', 'redis://localhost:6379/1')

    # Flask-Session (uses Redis when available, falls back to filesystem)
    SESSION_TYPE = 'redis' if os.getenv('REDIS_URL') else 'filesystem'
    SESSION_KEY_PREFIX = 'mcd:session:'
    SESSION_PERMANENT = True

    # Rate Limits
    RATELIMIT_DEFAULT = "5000 per day;500 per hour"
    RATELIMIT_STORAGE_URI = "memory://"  # Overridden at runtime if Redis available

    # Data paths
    DATA_DIR = os.getenv('DATA_DIR', 'data')

    _redis_ok = None
    _redis_checked_at = 0
    _REDIS_CHECK_TTL = 300  # Re-check every 5 minutes

    @staticmethod
    def _redis_available():
        """Check if Redis is reachable (cached with 5-min TTL)."""
        import time
        now = time.time()
        if Config._redis_ok is not None and (now - Config._redis_checked_at) < Config._REDIS_CHECK_TTL:
            return Config._redis_ok
        try:
            import redis
            url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
            r = redis.from_url(url, socket_connect_timeout=1)
            r.ping()
            Config._redis_ok = True
        except Exception:
            Config._redis_ok = False
        Config._redis_checked_at = now
        return Config._redis_ok


class TestConfig(Config):
    """Test configuration."""
    TESTING = True
    SECRET_KEY = 'test-secret-key-for-testing-only'
    SESSION_COOKIE_SECURE = False
    DATA_DIR = '/tmp/carrac-test-data'
    SESSION_TYPE = 'filesystem'
    REDIS_URL = ''  # Disable Redis in tests
    # Also disable the session DB Redis URL — carrac/ai/routes._redis_client()
    # falls back to REDIS_SESSION_URL when REDIS_URL is empty, which lets the
    # chat-slot lock land in a real Redis (db 1) when one happens to be running
    # locally. That lock has a 120s TTL and leaks across test methods because
    # streaming /api/ai/chat responses don't run their generator.finalize() in
    # the test client, so _release_chat_slot() never fires. Forcing both URLs
    # empty keeps the lock in the in-process _active_chats dict that conftest
    # already clears between fixtures.
    REDIS_SESSION_URL = ''
    # Disable Flask-Limiter in tests — the in-memory backend persists state
    # across tests in the same process, which causes legit per-blueprint
    # limits (e.g. AI bp at "5 per minute") to leak between unrelated test
    # methods and surface as spurious 429s. Flask-Limiter respects this flag.
    RATELIMIT_ENABLED = False
