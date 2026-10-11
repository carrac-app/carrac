"""The cache-integrity monitor must run on any checkout with no host layout."""
import importlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _reload(monkeypatch, **env):
    for name in ("CARRAC_DATA_DIR", "CARRAC_CACHE_BASELINE", "CARRAC_CACHE_STATE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    import scripts.cache_integrity_check as mod
    return importlib.reload(mod)


def test_defaults_live_inside_the_checkout(monkeypatch):
    mod = _reload(monkeypatch)
    try:
        assert mod.DATA_DIR == str(REPO / "data")
        assert mod.BASELINE_FILE.startswith(mod.DATA_DIR)
        assert mod.STATE_FILE.startswith(mod.DATA_DIR)
        assert "/opt/carrac" not in (mod.DATA_DIR, mod.BASELINE_FILE, mod.STATE_FILE)
    finally:
        _reload(monkeypatch)


def test_environment_overrides_every_path(monkeypatch, tmp_path):
    mod = _reload(
        monkeypatch,
        CARRAC_DATA_DIR=str(tmp_path / "d"),
        CARRAC_CACHE_BASELINE=str(tmp_path / "b.json"),
        CARRAC_CACHE_STATE=str(tmp_path / "s.json"),
    )
    try:
        assert mod.DATA_DIR == str(tmp_path / "d")
        assert mod.BASELINE_FILE == str(tmp_path / "b.json")
        assert mod.STATE_FILE == str(tmp_path / "s.json")
    finally:
        _reload(monkeypatch)
