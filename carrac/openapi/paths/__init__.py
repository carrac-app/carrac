"""Aggregate all path definitions.

Two sources are merged:
  1. Hand-written path modules (charts, plays, ...) — legacy format.
  2. Auto-generated paths from @spec-decorated view functions — preferred.

Auto-generated entries win on collision (per-method), so a route can be
migrated from hand-written to @spec one method at a time.
"""

from .charts import get_paths as _charts
from .scanner import get_paths as _scanner
from .plays import get_paths as _plays
from .alerts import get_paths as _alerts
from .journal import get_paths as _journal
from .sentiment import get_paths as _sentiment
from .reports import get_paths as _reports
from .drawings import get_paths as _drawings
from .quant import get_paths as _quant
from .ai import get_paths as _ai
from .composite import get_paths as _composite


def merge_all_paths():
    """Merge paths from hand-written modules + auto-gen @spec registry."""
    merged = {}
    for getter in (_charts, _scanner, _plays, _alerts, _journal,
                   _sentiment, _reports, _drawings, _quant, _ai, _composite):
        for path, methods in getter().items():
            if path in merged:
                merged[path].update(methods)
            else:
                merged[path] = methods

    # Layer auto-generated @spec entries on top (they win per-method).
    # build_auto_paths walks the app's url_map, so it needs an app context;
    # outside one (unit tests of the hand-written paths) those are the whole
    # output. Inside one, any failure propagates: swallowing it would publish
    # a document missing every @spec route.
    from flask import current_app, has_app_context
    if not has_app_context():
        return merged

    from ..spec_decorator import build_auto_paths
    auto = build_auto_paths(current_app._get_current_object())
    for path, methods in auto.items():
        if path in merged:
            merged[path].update(methods)
        else:
            merged[path] = methods

    return merged
