"""Framework-neutral identity resolvers for API keys + widget tokens.

These exist for the FastAPI strangler-fig migration so the
new ``carrac.fastapi_app.deps.auth`` module can verify keys and tokens
without importing Flask primitives. The Flask side
(``carrac/auth/decorators.py::verify_api_key``,
``carrac/widget/auth.py``) keeps its current shape — Flask handlers
still rely on the ``flask.g.api_key_*`` attribute writes that helper
performs as a side effect, and refactoring it risks
silently breaking 13 readers across MCP, alerts, plays, composite, etc.

Both the Flask helpers AND these pure resolvers consult the same
``user_db`` and the same ``SCREENSHOT_API_KEY`` env var, so they will
agree on every input.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("carrac.auth.pure")

# Sentinel returned for the shared admin key path (no per-user DB row).
SHARED_KEY_USER_ID: Optional[int] = None


def resolve_api_key(provided_key: str, *, allow_shared: bool = False) -> Optional[dict]:
    """Verify ``provided_key`` against shared + per-user paths.

    Returns ``None`` for invalid/empty input. Returns a dict with keys
    ``user_id`` (``None`` for shared admin key, ``int`` for per-user key),
    ``tier`` (str), and ``scope`` (``"full"`` | ``"read-only"``) on success.

    Mirrors ``carrac/auth/decorators.py::verify_api_key``
    minus the ``flask.g`` side-effects.

    ``allow_shared`` — the caller's declaration that the key
    arrived via the internal ``X-Internal-Key`` header. The shared admin key
    is honoured ONLY then; on the public ``X-API-Key`` header it is just an
    unknown string. This module is framework-neutral and cannot see headers
    itself, so the caller MUST compute this from the header the key actually
    came from, never pass ``True`` unconditionally. Defaults to the safe
    side. Per-user ``cvx_k_`` keys ignore the flag entirely.
    """
    if not provided_key:
        return None

    expected = os.getenv("SCREENSHOT_API_KEY", "")
    if allow_shared and expected and hmac.compare_digest(provided_key, expected):
        return {"user_id": SHARED_KEY_USER_ID, "tier": "admin", "scope": "full"}

    if not provided_key.startswith("cvx_k_"):
        return None

    try:
        from carrac.apikeys.auth import hash_api_key, validate_key_format
    except Exception as exc:  # noqa: BLE001
        logger.warning("apikeys.auth import failed: %s", exc)
        return None

    if not validate_key_format(provided_key):
        return None

    try:
        from user_db import user_db
        info = user_db.verify_api_key_token(hash_api_key(provided_key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify_api_key_token DB lookup failed: %s", exc)
        return None

    if not info:
        return None
    return {
        "user_id": info.get("user_id"),
        "tier": info.get("tier", "free") or "free",
        "scope": info.get("scope", "full") or "full",
        "is_internal": bool(info.get("is_internal", False)),
    }


def resolve_widget_token(token: str) -> Optional[dict[str, Any]]:
    """Verify a widget Bearer token. Returns ``None`` for invalid/missing.

    Returns ``{"user_id", "token_id", "tier", "rate_limited"}`` on success.
    The rate-limit check uses the same in-process sliding window the Flask
    decorator does (``carrac.widget.auth.check_rate_limit``); a returned
    ``rate_limited=True`` means the token is valid but over its 200/hour
    quota — callers should emit 429.
    """
    if not token:
        return None

    try:
        from carrac.widget.auth import (
            check_rate_limit,
            hash_token,
            validate_token_format,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("widget.auth import failed: %s", exc)
        return None

    if not validate_token_format(token):
        return None

    token_hash = hash_token(token)
    rate_limited = not check_rate_limit(token_hash)
    if rate_limited:
        # Still surface the limited flag so the dep can emit 429 with the
        # right message (token is valid, quota is exhausted).
        return {
            "user_id": None,
            "token_id": None,
            "tier": "free",
            "rate_limited": True,
        }

    try:
        from user_db import user_db
        info = user_db.verify_widget_token(token_hash)
    except Exception as exc:  # noqa: BLE001
        logger.warning("verify_widget_token DB lookup failed: %s", exc)
        return None

    if not info:
        return None
    return {
        "user_id": info.get("user_id"),
        "token_id": info.get("id"),
        "tier": info.get("tier", "free") or "free",
        "rate_limited": False,
    }


__all__ = [
    "SHARED_KEY_USER_ID",
    "resolve_api_key",
    "resolve_widget_token",
]
