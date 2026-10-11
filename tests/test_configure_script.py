"""scripts/configure.sh — the optional-keys helper.

It upserts into .env without the user editing the file by hand: an
uncommented key gets its value replaced, a commented-out `# KEY=` line is
uncommented, an unknown key is appended, and an empty value is refused
(an empty `KEY=` reaches the app as "" which is not the same as unset).
`--list` reports which optional features are on without printing a secret.
The interactive mode is driven here through stdin.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "configure.sh"


def run(args, cwd, stdin=""):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=cwd, input=stdin, text=True, capture_output=True, timeout=30,
        env={**os.environ, "NO_COLOR": "1"},
    )


@pytest.fixture
def env_dir(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env")
    return tmp_path


def read_env(d):
    return (d / ".env").read_text(encoding="utf-8")


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), "scripts/configure.sh is missing"
    assert os.access(SCRIPT, os.X_OK), "scripts/configure.sh is not executable"


def test_set_replaces_an_uncommented_key(env_dir):
    r = run(["--set", "SUPPORT_EMAIL=help@example.org"], env_dir)
    assert r.returncode == 0, r.stderr
    env = read_env(env_dir)
    assert "\nSUPPORT_EMAIL=help@example.org\n" in env
    assert env.count("SUPPORT_EMAIL=") == 1


def test_set_uncomments_a_documented_optional_key(env_dir):
    assert "\n# FRED_API_KEY=\n" in read_env(env_dir)
    r = run(["--set", "FRED_API_KEY=abc123"], env_dir)
    assert r.returncode == 0, r.stderr
    env = read_env(env_dir)
    assert "\nFRED_API_KEY=abc123\n" in env
    assert "# FRED_API_KEY=" not in env


def test_set_appends_an_unknown_key(env_dir):
    r = run(["--set", "MY_EXTRA_FLAG=1"], env_dir)
    assert r.returncode == 0, r.stderr
    assert read_env(env_dir).rstrip("\n").endswith("MY_EXTRA_FLAG=1")


def test_set_refuses_an_empty_value_and_leaves_the_file_alone(env_dir):
    before = read_env(env_dir)
    r = run(["--set", "FRED_API_KEY="], env_dir)
    assert r.returncode != 0
    assert "empty" in (r.stderr + r.stdout).lower()
    assert read_env(env_dir) == before


def test_set_keeps_other_lines_byte_for_byte(env_dir):
    before = read_env(env_dir).splitlines()
    run(["--set", "FINNHUB_API_KEY=k"], env_dir)
    after = read_env(env_dir).splitlines()
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert changed == [("# FINNHUB_API_KEY=", "FINNHUB_API_KEY=k")]
    assert len(before) == len(after)


def test_list_reports_features_without_printing_secrets(env_dir):
    run(["--set", "FRED_API_KEY=supersecretvalue", "ANTHROPIC_API_KEY=sk-ant-hidden"], env_dir)
    r = run(["--list"], env_dir)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "supersecretvalue" not in out and "sk-ant-hidden" not in out
    assert "FRED_API_KEY" in out and "ANTHROPIC_API_KEY" in out
    # on/off is visible per key
    assert "set" in out.lower() and "unset" in out.lower()


def test_interactive_writes_only_the_answered_group(env_dir):
    # the required-values offer comes first (the copied example still has
    # placeholders), then the nine groups in a fixed order; answer "n" to
    # everything except market data, give a FRED key, leave Finnhub empty.
    answers = "\n".join(["n", "n", "n", "y", "fredkey42", "", "n", "n", "n", "n", "n", "n"]) + "\n"
    r = run([], env_dir, stdin=answers)
    assert r.returncode == 0, r.stderr + r.stdout
    env = read_env(env_dir)
    assert "\nFRED_API_KEY=fredkey42\n" in env
    assert "\n# FINNHUB_API_KEY=\n" in env, "an empty answer must leave the key commented out"
    assert "force-recreate" in r.stdout, "the user must be told how to apply the change"


def test_missing_env_file_is_created_from_the_example(env_dir):
    (env_dir / ".env").unlink()
    r = run(["--set", "FRED_API_KEY=x"], env_dir)
    assert r.returncode == 0, r.stderr
    assert (env_dir / ".env").is_file()
    assert "\nFRED_API_KEY=x\n" in read_env(env_dir)
