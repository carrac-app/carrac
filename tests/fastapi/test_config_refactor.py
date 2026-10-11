"""Config-refactor parity tests.

Each test imports a helper that once read ``current_app.config`` and
verifies it now resolves env vars in a Flask-app-context-free interpreter.
"""

from __future__ import annotations




def test_telegram_no_flask(monkeypatch, real_notification_senders):
    # real_notification_senders: the point of this test is that the GENUINE
    # sender runs outside a Flask app context, so the notification-send recorder would
    # make it pass for the wrong reason. Blank credentials keep it a no-op.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    from carrac.notifications.telegram import send_telegram_message

    send_telegram_message("hello")  # must not raise RuntimeError


def test_anthropic_provider_model_from_env(monkeypatch):
    monkeypatch.setenv("AI_MODEL", "claude-sonnet-4-6")
    import carrac.llm.anthropic as m

    p = m.AnthropicProvider()
    assert p.model == "claude-sonnet-4-6"


def test_anthropic_provider_model_falls_back_to_init(monkeypatch):
    monkeypatch.delenv("AI_MODEL", raising=False)
    import carrac.llm.anthropic as m

    p = m.AnthropicProvider(model="claude-opus-4-6")
    assert p.model == "claude-opus-4-6"


def test_openai_compat_properties(monkeypatch):
    monkeypatch.setenv("AI_MODEL", "gpt-test-model")
    monkeypatch.setenv("AI_API_KEY", "sk-test-key")
    monkeypatch.setenv("AI_BASE_URL", "https://api.openai.com/v1/chat/completions")
    import carrac.llm.openai_compat as m

    p = m.OpenAICompatProvider()
    assert p.model == "gpt-test-model"
    assert p._api_key == "sk-test-key"
    assert p._base_url == "https://api.openai.com/v1/chat/completions"


def test_openai_compat_base_url_default(monkeypatch):
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    import carrac.llm.openai_compat as m

    p = m.OpenAICompatProvider()
    assert p._base_url == "https://api.x.ai/v1/chat/completions"


def test_current_provider_no_flask(monkeypatch):
    monkeypatch.setenv("AI_MODEL", "claude-sonnet-4-6")
    from carrac.ai.anthropic_client import _current_provider

    p = _current_provider()
    assert p._model == "claude-sonnet-4-6"


def test_billing_redis_client_returns_none_when_unset(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    import carrac.ai.billing as b

    b._billing_redis_client = None
    b._billing_redis_url = None
    assert b._redis_client() is None


def test_daily_cap_cents_from_env(monkeypatch):
    monkeypatch.setenv("AI_GLOBAL_DAILY_USD_CAP", "50")
    from carrac.ai.billing import _daily_cap_cents

    assert _daily_cap_cents() == 5000


def test_daily_cap_cents_default(monkeypatch):
    monkeypatch.delenv("AI_GLOBAL_DAILY_USD_CAP", raising=False)
    from carrac.ai.billing import _daily_cap_cents

    assert _daily_cap_cents() == 3000


def test_calculate_cost_cents_no_flask(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_MODEL", "gpt-4")
    monkeypatch.setenv("AI_INPUT_RATE", "10.0")
    monkeypatch.setenv("AI_OUTPUT_RATE", "30.0")
    from carrac.ai.billing import calculate_cost_cents

    # 1M input * 10 + 1M output * 30 = $40 → 4000 cents
    assert calculate_cost_cents(1_000_000, 1_000_000) == 4000


def test_get_credit_markup_default(monkeypatch):
    """Default flips from 4.0 (old) to 3.0 — matches Config.CREDIT_MARKUP."""
    monkeypatch.delenv("CREDIT_MARKUP", raising=False)
    from carrac.ai.billing import get_credit_markup

    assert get_credit_markup() == 3.0


def test_get_credit_markup_from_env(monkeypatch):
    monkeypatch.setenv("CREDIT_MARKUP", "3.5")
    from carrac.ai.billing import get_credit_markup

    assert get_credit_markup() == 3.5


def test_llm_get_provider_explicit(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    from carrac.ai.llm_client import _get_provider

    assert _get_provider() == "anthropic"


def test_llm_get_provider_autodetect_claude(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    monkeypatch.setenv("AI_MODEL", "claude-sonnet-4-6")
    from carrac.ai.llm_client import _get_provider

    assert _get_provider() == "anthropic"


def test_journal_ai_review_imports_no_flask():
    """Module-level import must not require a Flask app context."""
    import carrac.journal.ai_review  # noqa: F401


def test_journal_screenshots_imports_no_flask():
    import carrac.journal.screenshots  # noqa: F401


def test_mcp_charts_imports_no_flask(monkeypatch):
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-key")
    import carrac.mcp.tools.charts  # noqa: F401


def test_mcp_market_imports_no_flask():
    import carrac.mcp.tools.market  # noqa: F401


def test_verify_api_key_admin_path_uses_env(monkeypatch):
    """verify_api_key admin-key branch reads SCREENSHOT_API_KEY from env.

    The admin-key path now also checks that the shared key
    arrived via the ``X-Internal-Key`` header (it reads the header directly
    off the current request, regardless of which header ``provided_key`` was
    extracted from) — so a request context carrying that header is needed,
    not just an app context.
    """
    import flask

    monkeypatch.setenv("SCREENSHOT_API_KEY", "admin-key-xyz")
    test_app = flask.Flask(__name__)
    with test_app.test_request_context(headers={"X-Internal-Key": "admin-key-xyz"}):
        from carrac.auth.decorators import verify_api_key

        assert verify_api_key("admin-key-xyz") is True
        assert verify_api_key("wrong-key") is False


def test_reports_screenshots_imports_no_flask():
    """Bracket access replaced with os.getenv — no KeyError when env unset."""
    import carrac.reports.screenshots  # noqa: F401
