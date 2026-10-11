"""scripts/quickstart.sh must generate every secret a fresh stack needs."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "quickstart.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None or shutil.which("bash") is None,
    reason="openssl and bash are required",
)


def _generate_env(tmp_path):
    """Run generate_env from quickstart.sh against a throwaway .env.example."""
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    text = SCRIPT.read_text()
    start = text.index("generate_password() {")
    end = text.index("# ----", text.index("generate_env() {"))
    harness = (
        "set -euo pipefail\n"
        "preflight_fail() { echo \"$1\" >&2; exit 1; }\n"
        'ENV_FILE=".env"\n' + text[start:end] + "\ngenerate_env >/dev/null\n"
    )
    subprocess.run(["bash", "-c", harness], cwd=tmp_path, check=True)
    values = {}
    for line in (tmp_path / ".env").read_text().splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def test_generates_telegram_token_encryption_key_as_a_fernet_key(tmp_path):
    from cryptography.fernet import Fernet

    key = _generate_env(tmp_path).get("TELEGRAM_TOKEN_ENCRYPTION_KEY", "")
    assert key, "TELEGRAM_TOKEN_ENCRYPTION_KEY was not generated"
    Fernet(key.encode())  # raises on a malformed key


def test_generates_internal_screenshot_api_key(tmp_path):
    key = _generate_env(tmp_path).get("SCREENSHOT_API_KEY", "")
    assert re.fullmatch(r"[0-9a-f]{64}", key), "SCREENSHOT_API_KEY was not generated"
