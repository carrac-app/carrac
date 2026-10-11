"""Pydantic models for the widget-token write surface.

``POST /api/widget/tokens``,
``DELETE /api/widget/tokens/{token_id}``.

Widget token writes have NO X-API-Key bypass for CSRF:
``/api/widget/tokens`` is excluded from ``CarracCSRFMiddleware``'s X-API-Key
bypass branch, so session callers are always checked.
These models enforce nothing extra — security lives in the middleware.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WidgetTokenCreateRequest(BaseModel):
    """Body for ``POST /api/widget/tokens``.

    ``name`` defaults to ``"Default"``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Default", max_length=50)


__all__ = [
    "WidgetTokenCreateRequest",
]
