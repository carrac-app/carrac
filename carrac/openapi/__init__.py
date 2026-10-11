"""
Carrac OpenAPI 3.0 specification generator.

Sources:
  - Hand-written paths in carrac.openapi.paths.*  (legacy)
  - Auto-generated paths from @spec decorators on view functions  (preferred)

Auto-gen wins on path+method collision, so migration is per-operation.
Spec is cached per-worker (only changes on deploy/restart).
"""

# NOTE: Do NOT re-export `spec` here — the name collides with the
# submodule `carrac.openapi.spec` (hand-written metadata). Importing
# as `from carrac.openapi import spec` sometimes resolves to the
# module instead of the decorator, depending on import order.
# Use `from carrac.openapi.spec_decorator import spec` instead.

from flask import has_app_context

API_VERSION = '1'

_cached_spec = None


def build_spec():
    """Build and cache the full OpenAPI 3.0 spec dict."""
    global _cached_spec
    if _cached_spec is not None:
        return _cached_spec

    from .spec import get_metadata
    from .security import get_security_schemes
    from .schemas import get_component_schemas
    from .paths import merge_all_paths

    meta = get_metadata()
    meta['paths'] = merge_all_paths()
    meta['components'] = {
        'securitySchemes': get_security_schemes(),
        'schemas': get_component_schemas(),
    }
    # Outside an app context the @spec layer is skipped (see merge_all_paths),
    # so that partial document must not be served from the cache later.
    if has_app_context():
        _cached_spec = meta
    return meta


def reset_spec_cache():
    """Clear cached spec (for testing / after route registration changes)."""
    global _cached_spec
    _cached_spec = None


__all__ = ['build_spec', 'reset_spec_cache', 'API_VERSION']
