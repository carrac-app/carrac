"""
Carrac changelog — user-facing release notes.
Keep entries non-technical, no pricing info, no implementation details.
"""

import json
import logging
import os

from carrac.version import APP_VERSION  # noqa: F401 — re-exported for `from changelog import APP_VERSION`

_logger = logging.getLogger(__name__)

_DATA_FILE = os.path.join(os.path.dirname(__file__), "changelog_data.json")

try:
    with open(_DATA_FILE, encoding="utf-8") as _f:
        CHANGELOG = json.load(_f)
except Exception as _e:  # pragma: no cover
    _logger.warning("changelog_data.json could not be loaded (%s) — CHANGELOG will be empty", _e)
    CHANGELOG = []
