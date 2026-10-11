"""Preferences write routing tests.

Confirms:
- All three preference endpoints are registered in ``MIGRATED_WRITE_PATHS``.
- Method filters work correctly (POST → FastAPI, GET → Flask).
- Pydantic models enforce ``extra="forbid"``.
- CSRF is skipped for ``/api/preferences`` and ``/api/settings`` paths
  (``charts_bp`` is ``csrf.exempt`` in Flask — middleware mirrors this).
"""

from __future__ import annotations

import pytest

from carrac.asgi import (
    MIGRATED_WRITE_PATHS,
    ExactPath,
)
from carrac.fastapi_app.models.preferences import (
    IndicatorPrefsRequest,
    PreferencesSyncRequest,
    SettingsSaveRequest,
)


# ---------------------------------------------------------------------------
# Dispatcher registration
# ---------------------------------------------------------------------------


def test_indicator_prefs_listed_in_migrated_writes():
    assert ("/api/indicator-prefs", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_preferences_sync_listed_in_migrated_writes():
    assert ("/api/preferences/sync", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_settings_save_listed_in_migrated_writes():
    assert ("/api/settings/save", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


# ---------------------------------------------------------------------------
# Method-filter sanity
# ---------------------------------------------------------------------------


def test_indicator_prefs_post_only():
    ep = ExactPath("/api/indicator-prefs", app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": "/api/indicator-prefs"}

    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})

    assert post_match.value > 0
    assert get_match.value == 0


def test_preferences_sync_post_only():
    ep = ExactPath("/api/preferences/sync", app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": "/api/preferences/sync"}

    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})

    assert post_match.value > 0
    assert get_match.value == 0


def test_settings_save_post_only():
    ep = ExactPath("/api/settings/save", app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": "/api/settings/save"}

    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})

    assert post_match.value > 0
    assert get_match.value == 0


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


def test_indicator_prefs_request_all_optional():
    """All fields optional — empty body is valid."""
    req = IndicatorPrefsRequest()
    assert req.indicators is None
    assert req.favourites is None
    assert req.groups is None
    assert req.params is None


def test_indicator_prefs_request_extra_field_rejected():
    with pytest.raises(Exception):
        IndicatorPrefsRequest(indicators={"RSI": True}, rogue="bad")


def test_indicator_prefs_request_partial_body():
    req = IndicatorPrefsRequest(indicators={"RSI": True})
    assert req.indicators == {"RSI": True}
    assert req.favourites is None


def test_preferences_sync_request_requires_key_and_value():
    req = PreferencesSyncRequest(key="display_options", value={"greenRedCandles": True})
    assert req.key == "display_options"
    assert req.value == {"greenRedCandles": True}


def test_preferences_sync_request_extra_field_rejected():
    with pytest.raises(Exception):
        PreferencesSyncRequest(key="display_options", value={}, rogue="bad")


def test_settings_save_request_requires_key_and_value():
    req = SettingsSaveRequest(key="chart_prefs", value={"lastViewedAsset": "BTC"})
    assert req.key == "chart_prefs"


def test_settings_save_request_extra_field_rejected():
    with pytest.raises(Exception):
        SettingsSaveRequest(key="chart_prefs", value={}, rogue="bad")


# ---------------------------------------------------------------------------
# CSRF skip assertion (structural)
# ---------------------------------------------------------------------------


def test_csrf_middleware_skips_preferences_paths():
    """``/api/preferences`` and ``/api/settings`` are in ``_SKIP_PREFIXES``
    because ``charts_bp`` is ``csrf.exempt`` in Flask.
    """
    from carrac.fastapi_app.csrf_middleware import _SKIP_PREFIXES

    # At least one prefix must cover /api/preferences/sync
    prefs_covered = any(
        "/api/preferences/sync".startswith(p) for p in _SKIP_PREFIXES
    )
    assert prefs_covered, "/api/preferences/sync should be in a CSRF skip prefix"

    # At least one prefix must cover /api/settings/save
    settings_covered = any(
        "/api/settings/save".startswith(p) for p in _SKIP_PREFIXES
    )
    assert settings_covered, "/api/settings/save should be in a CSRF skip prefix"
