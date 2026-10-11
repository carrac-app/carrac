"""Dependency lockfile CI gate.

Guards the requirements.txt ↔ requirements.lock contract:

  * requirements.lock exists and contains only exact ``name==version`` pins
    (comments / blank lines allowed) — no floors, no direct refs.
  * Every requirement declared in requirements.txt resolves to a locked
    version that satisfies its specifier, so the lock can never silently
    drift from the declared constraints (and vice versa: tightening a pin
    in requirements.txt forces a lock regen).
  * No duplicate entries in the lock.

The prod image installs from requirements.lock (Dockerfile), so a fresh-box
rebuild reproduces the exact package set verified in production. CI installs
the lock on its Python 3.11 leg, the version the image runs, and the loose
requirements on the 3.14 leg, where the lock cannot resolve.

Regenerating the lock (documented in the lock header too):
  docker run --rm -v "$PWD/requirements.txt:/r.txt:ro" python:3.11-slim \
      sh -c "pip install -q -r /r.txt >/dev/null 2>&1 && pip freeze"
"""

from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
LOCK = REPO_ROOT / "requirements.lock"

_EXACT_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[A-Za-z0-9.!+_-]+)$")


def _lock_lines() -> list[str]:
    lines = []
    for raw in LOCK.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _lock_pins() -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for line in _lock_lines():
        m = _EXACT_PIN.match(line)
        assert m, f"requirements.lock line is not an exact name==version pin: {line!r}"
        pins[canonicalize_name(m.group("name"))] = Version(m.group("version"))
    return pins


def _declared_requirements() -> list[Requirement]:
    reqs = []
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            reqs.append(Requirement(line))
    return reqs


def test_lock_exists():
    assert LOCK.is_file(), (
        "requirements.lock is missing — regenerate it (see this file's "
        "docstring) and commit it alongside requirements.txt"
    )


def test_lock_contains_only_exact_pins():
    for line in _lock_lines():
        assert _EXACT_PIN.match(line), (
            f"requirements.lock must contain only exact name==version pins; "
            f"offending line: {line!r}"
        )


def test_lock_has_no_duplicate_packages():
    names = [canonicalize_name(_EXACT_PIN.match(l).group("name")) for l in _lock_lines()]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate packages in requirements.lock: {sorted(dupes)}"


def test_every_requirement_satisfied_by_lock():
    pins = _lock_pins()
    problems = []
    for req in _declared_requirements():
        name = canonicalize_name(req.name)
        if name not in pins:
            problems.append(f"{req.name}: declared in requirements.txt but absent from lock")
            continue
        if req.specifier and not req.specifier.contains(pins[name], prereleases=True):
            problems.append(
                f"{req.name}: locked at {pins[name]} which violates "
                f"requirements.txt specifier {req.specifier!s}"
            )
    assert not problems, (
        "requirements.txt ↔ requirements.lock drift (regenerate the lock or "
        "fix the pin):\n  " + "\n  ".join(problems)
    )
