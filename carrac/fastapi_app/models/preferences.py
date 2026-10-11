"""Pydantic models for the preferences write surface.

``POST /api/indicator-prefs``,
``POST /api/preferences/sync``, ``POST /api/settings/save``.

``/api/preferences`` and ``/api/settings`` are in the CSRF middleware's
``_SKIP_PREFIXES`` list so there is NO CSRF
enforcement on these paths.

``IndicatorPrefsRequest``: merge semantics.  The handler merges each
present field into the stored prefs dict — it does NOT replace the whole
object.

``PreferencesSyncRequest`` / ``SettingsSaveRequest``: share the same wire
shape (``key`` + ``value``).  Per-key semantic validation (report_indicators
filtering, report_assets shape, etc.) is delegated to the handler.  The ``key`` field is typed as a ``str`` with
a whitelist check in the handler rather than a ``Literal`` union to keep
field validation errors as 400 (not 422) via the shared exception handler.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class IndicatorPrefsRequest(BaseModel):
    """Body for ``POST /api/indicator-prefs``.

    All fields optional — absent keys are not merged (no-op for that key).
    """

    model_config = ConfigDict(extra="forbid")

    indicators: Optional[dict] = None
    favourites: Optional[list] = None
    groups: Optional[list] = None
    params: Optional[dict] = None


class PreferencesSyncRequest(BaseModel):
    """Body for ``POST /api/preferences/sync``.

    Deep semantic validation (report_indicators list filter, report_assets
    shape, column_prefs whitelist, report_schedule region) is performed
    inline in the handler.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    value: Any


class SettingsSaveRequest(BaseModel):
    """Body for ``POST /api/settings/save``.

    Identical wire shape to ``PreferencesSyncRequest``.  Separate model so
    route declarations are unambiguous and error messages name the correct
    endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    value: Any


__all__ = [
    "IndicatorPrefsRequest",
    "PreferencesSyncRequest",
    "SettingsSaveRequest",
]
