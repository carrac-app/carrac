"""The suite must be sealed from a developer's local .env file."""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(tmp_path, extra_env):
    (tmp_path / ".env").write_text("FRED_API_KEY=should-not-leak\n")
    env = {k: v for k, v in os.environ.items() if k != "FRED_API_KEY"}
    env.pop("CARRAC_SKIP_DOTENV", None)
    env.update(extra_env)
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [sys.executable, "-c",
         "import os, carrac.config; print(repr(os.environ.get('FRED_API_KEY')))"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )


def test_dotenv_is_read_by_default(tmp_path):
    """Baseline: without the opt-out the loader does read ./.env."""
    out = _run(tmp_path, {})
    assert out.returncode == 0, out.stderr
    assert "should-not-leak" in out.stdout


def test_skip_dotenv_opt_out_keeps_key_out(tmp_path):
    out = _run(tmp_path, {"CARRAC_SKIP_DOTENV": "1"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "None"


def test_suite_environment_has_no_provider_keys():
    """conftest strips provider and secret keys before anything imports carrac."""
    for name in ("FRED_API_KEY", "FINNHUB_API_KEY", "ANTHROPIC_API_KEY",
                 "AI_API_KEY", "SENTRY_DSN", "SENTRY_BROWSER_DSN",
                 "TELEGRAM_TOKEN_ENCRYPTION_KEY"):
        assert not os.environ.get(name), name
    assert not [k for k in os.environ
                if k.startswith(("BREVO_SMTP_", "PADDLE_", "VAPID_"))]
