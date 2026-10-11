"""
MCP server package.

The MCP HTTP surface (POST /mcp) is served exclusively
by the FastAPI router at carrac/fastapi_app/routers/mcp.py, which
shares the same dispatch path via carrac.mcp.runtime + protocol.

Submodules retained:
- protocol — JSON-RPC dispatch (runtime-agnostic)
- registry — tool registry (TOOL_REGISTRY)
- runtime — contextvar-based caller plumbing
- rate_limit — per-key rate limiting
- tools — tool implementations (registers via import side effects)
- indicator_registry_parser — schema helper used by tools
"""

__all__: list[str] = []
