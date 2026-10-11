"""quickstart.sh must read API JSON by parsing it, not by matching its spacing."""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "quickstart.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


def _field(body, *args):
    """Run quickstart.sh's json_field against `body` with the host interpreter."""
    text = SCRIPT.read_text()
    start = text.index("JSON_FIELD_PY='")
    end = text.index("# The first top-level `<div class=\"error-message\">")
    harness = (
        "set -euo pipefail\n"
        'json_python() { python3 "$@"; }\n'
        'TMP_BODY="$1"; shift\n' + text[start:end] + '\njson_field "$@"\n'
    )
    return subprocess.run(
        ["bash", "-c", harness, "bash", body, *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.mark.parametrize(
    "payload",
    [
        '{"success":true,"count":1,"drawings":[]}',
        '{"success": true, "count": 1, "drawings": []}',
        '{\n  "success": true,\n  "count":  1\n}',
    ],
    ids=["compact", "spaced", "pretty"],
)
def test_number_field_ignores_spacing(tmp_path, payload):
    body = tmp_path / "body.json"
    body.write_text(payload)
    assert _field(str(body), "count") == "1"


def test_string_and_nested_fields(tmp_path):
    body = tmp_path / "body.json"
    body.write_text('{"key": "ck_abc", "result": {"tools": [{"inputSchema": {}}, {"inputSchema": {}}]}}')
    assert _field(str(body), "key") == "ck_abc"
    assert _field(str(body), "result.tools", "len") == "2"


def test_missing_key_and_bad_json_print_nothing(tmp_path):
    body = tmp_path / "body.json"
    body.write_text('{"count": 1}')
    assert _field(str(body), "drawing_id") == ""
    body.write_text("<html>not json</html>")
    assert _field(str(body), "count") == ""
