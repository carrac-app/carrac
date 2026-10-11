"""Import all tool modules to register them with the MCP tool registry."""


def get_mcp_user_id():
    """Return the authenticated user ID for the current MCP request.

    Raises ToolError if the caller authenticated with a shared/admin key that
    has no per-user binding. This prevents user-scoped tools (plays, journal,
    alerts, positions, trader profile) from silently reading or writing data
    on the admin's account.

    Tools that do NOT scope to a user (market, guide, scanner) must not call
    this helper.

    Reads via carrac.mcp.runtime so this works under both the
    Flask blueprint and the FastAPI router. Flask handler still populates
    ``flask.g`` for legacy callers, so behaviour is unchanged there.
    """
    from carrac.mcp.protocol import ToolError
    from carrac.mcp.runtime import get_mcp_caller_user_id

    uid = get_mcp_caller_user_id()
    if uid is None:
        raise ToolError(
            'This tool requires a per-user API key. The shared admin key '
            'has no per-user binding and cannot access user-scoped data.'
        )
    return uid


# Batch 2: Read-only tools
from . import market    # noqa: F401
from . import scanner   # noqa: F401
from . import context   # noqa: F401

# Batch 3: Write/user-scoped tools
from . import plays     # noqa: F401
from . import journal   # noqa: F401
from . import analysis  # noqa: F401
from . import alerts    # noqa: F401

# Batch 4: Content tools
from . import guide     # noqa: F401

# Batch 5: Quant engine tools (derivatives, lead-lag, chains, analogues)
from . import quant     # noqa: F401

# Batch 6: Indicators & formula tools
from . import indicators  # noqa: F401

# Batch 7: Backtest composition
from . import backtest    # noqa: F401

# Batch 8: Regime reactivity
from . import regime      # noqa: F401

# Batch 9: Cross-system bridges
from . import bridges     # noqa: F401

# Batch 10: Report extensions
from . import reports     # noqa: F401

# Batch 11 (mcp-drop-1): Drawing CRUD, watchlist, journal list+update, notifications, quant anomalies
from . import drawings    # noqa: F401
from . import watchlist   # noqa: F401

# Batch 12 (mcp-drop-2): Precision timestamps, chart screenshot, volume profile,
#                         scenario children, trading profile, bulk archive, AI recipes
from . import timestamps  # noqa: F401
from . import charts      # noqa: F401
from . import profile     # noqa: F401
from . import recipes     # noqa: F401

# Backtest run history
from . import backtests   # noqa: F401

# Batch 14: get_started orientation bootstrap
from . import onboarding  # noqa: F401

# Batch 15 (ONCHAIN-1): On-chain smart-money positioning (Hyperliquid)
from . import onchain  # noqa: F401

# Batch 16: consolidated share/unshare across chart/play/trade
from . import sharing  # noqa: F401
