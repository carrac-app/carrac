"""Every shipped script answers --help with its usage and nothing else.

A script that ignores --help and runs anyway can write to a database, send an
alert or edit a crontab for someone who only wanted to read how it works.
Each one is run from an empty directory with a bare environment, so a script
that does act would fail here or leave a file behind.
"""

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = sorted(
    p for p in (ROOT / "scripts").iterdir()
    if p.suffix in {".py", ".sh"} and p.name != "__init__.py"
)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_help_prints_usage_and_does_nothing(script, tmp_path):
    cmd = [sys.executable if script.suffix == ".py" else "bash", str(script), "--help"]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip(), "--help printed nothing on stdout"
    assert list(tmp_path.iterdir()) == [], "--help left files behind"
