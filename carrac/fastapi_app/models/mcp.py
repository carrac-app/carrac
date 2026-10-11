"""Pydantic models for the MCP JSON-RPC 2.0 endpoint.

The body shape mirrors what ``carrac.mcp.protocol.dispatch`` already
accepts. We keep the model intentionally permissive on ``params``
(``Any`` instead of a per-method discriminated union) because per-method
parameter shapes are validated inside the dispatcher itself
(``InvalidParams`` errors are returned as JSON-RPC -32602 responses).

What the model DOES enforce:
* ``jsonrpc`` must literally equal ``"2.0"`` (rejected upstream by the
  dispatcher with -32600 if absent — Pydantic catches the structural
  cases first with a 400).
* ``method`` is a non-empty string.
* ``id`` is ``str | int | None`` (notifications have no id; calls have
  one). JSON-RPC 2.0 §4.2 also permits ``null`` and floats; we accept
  ``None`` and stringify floats client-side.

Rejecting unknown top-level fields (``extra="forbid"``) catches typoed
keys early — the JSON-RPC spec doesn't define extras at the envelope
level, and silently ignoring them has bitten us before.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class MCPRequest(BaseModel):
    """JSON-RPC 2.0 request envelope for ``POST /mcp``."""

    model_config = ConfigDict(extra="forbid")

    jsonrpc: str = Field(default="2.0")
    method: str = Field(..., min_length=1, max_length=80)
    params: Optional[Any] = None
    # JSON-RPC 2.0: id is str | int | null. Notifications omit id entirely.
    id: Optional[Union[str, int]] = None
