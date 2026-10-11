"""
Compat shim over the ServiceContainer.

Preferred access:

    from carrac.services import get_services
    get_services().db

Legacy ``svc.db`` / ``carrac.services.db`` style access continues to work
via the module-level ``__getattr__`` hook below. The container lives in two
mirrored locations:

    * ``app.extensions['carrac_services']`` — installed by
      ``create_app()``; preferred when a Flask app context is active.
    * ``_fallback_container`` in this module — used by non-Flask contexts
      (``worker.py``, quant collectors, morning engine, CLI scripts).

Both pointers reference the same instance, so there is no divergence.
"""

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from carrac.service_container import ServiceContainer  # noqa: F401

logger = logging.getLogger('carrac.services')

_fallback_container: "Optional[ServiceContainer]" = None

_SERVICE_NAMES = frozenset({
    'db', 'user_db', 'consensio', 'rsi_calc', 'ema_ribbon_calc',
    'trend_analyzer', 'divergence_detector_daily', 'divergence_detector_weekly',
    'adx_calc', 'weekly_signal_engine', 'cpr_calc',
    'opportunity_scorer', 'research_scorer', 'user_scorer',
    'ema_focused_scorer', 'balanced_scorer', 'structure_analyzer', 'bb_calc',
})


def init_services() -> "ServiceContainer":
    """Build the ServiceContainer and install it as the module fallback.

    Idempotent — safe to call multiple times. Called by ``create_app()`` before
    blueprint registration, and by the worker at start-up.
    """
    global _fallback_container
    if _fallback_container is not None:
        return _fallback_container
    from carrac.service_container import ServiceContainer
    _fallback_container = ServiceContainer.build()
    logger.info("All service singletons initialized")
    return _fallback_container


def get_services(app=None) -> "ServiceContainer":
    """Return the active ServiceContainer.

    Resolution order:
        1. Explicit ``app`` argument → ``app.extensions``.
        2. Flask ``current_app`` if a request/app context is active.
        3. Module-level ``_fallback_container``.

    Raises RuntimeError if no container has been built yet.
    """
    if app is not None:
        container = app.extensions.get('carrac_services')
        if container is not None:
            return container

    try:
        from flask import current_app, has_app_context
        if has_app_context():
            container = current_app.extensions.get('carrac_services')
            if container is not None:
                return container
    except (RuntimeError, ImportError):
        pass

    if _fallback_container is None:
        raise RuntimeError(
            "ServiceContainer not initialized. Call init_services() or "
            "create_app() before accessing services."
        )
    return _fallback_container


def install_on_app(app) -> "ServiceContainer":
    """Attach the active container to ``app.extensions``.

    Called from ``create_app()``. Builds the container via
    ``init_services()`` if needed, so both the app-bound pointer and the
    module-level fallback reference the same instance.
    """
    container = init_services()
    app.extensions['carrac_services'] = container
    return container


def __getattr__(name: str) -> Any:
    """Legacy module-attribute shim.

    Resolves ``carrac.services.db`` (and peers) to the active container
    field. Invoked only when normal attribute lookup fails — the old
    module-level globals (``db = None`` etc.) are deliberately gone.
    """
    if name in _SERVICE_NAMES:
        return getattr(get_services(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
