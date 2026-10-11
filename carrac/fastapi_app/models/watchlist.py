"""Pydantic models for the watchlist write surface.

``POST /api/watchlist*``.

``POST /api/watchlist`` is unusual: the wire body is EITHER a flat
``list[str]`` (new format) OR a legacy grouped ``dict``.  Pydantic's Union
discriminator won't work cleanly on raw root types, so we accept both via
``body: list[str] | dict = Body(...)`` at the route level rather than a
BaseModel wrapper.  The three remaining endpoints (add/remove/reorder) have
conventional body shapes and use ``extra="forbid"`` models.
"""

from __future__ import annotations


from pydantic import BaseModel, ConfigDict, Field, field_validator


class WatchlistSymbolRequest(BaseModel):
    """Body for ``POST /api/watchlist/add`` and ``POST /api/watchlist/remove``."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=30)

    @field_validator("symbol", mode="before")
    @classmethod
    def strip_and_upper(cls, v: object) -> str:
        if isinstance(v, str):
            return v.strip().upper()
        return str(v).strip().upper()


__all__ = [
    "WatchlistSymbolRequest",
]
