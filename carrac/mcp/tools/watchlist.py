"""MCP tools: Watchlist management (1 tool)."""

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id

_MAX_SYMBOLS = 200
_MAX_SYMBOL_LEN = 20


@mcp_tool(
    'watchlist_manage',
    'Read or modify the authenticated user\'s watchlist (the list of asset symbols '
    'shown in the Carrac sidebar and scanner). '
    'Supported actions: '
    'list — return current symbols and count (no side effects); '
    'add — append a symbol (uppercased, no duplicates, max 200 symbols, max 20-char symbol); '
    'remove — drop a symbol by name; '
    'reorder — replace the full ordered list in one call (de-dupes while preserving order). '
    'All write actions (add/remove/reorder) persist immediately to the user DB. '
    'Returns {symbols, count} after every action, plus added/removed key on the '
    'relevant single-symbol operations (add returns already_present instead of '
    'added when the symbol was already on the list — nothing was changed). '
    'SIDE EFFECT: add/remove/reorder write to the user preferences store. '
    'EXAMPLE add: {"action": "add", "symbol": "SOL"} '
    'EXAMPLE reorder: {"action": "reorder", "symbols": ["BTC", "ETH", "SOL"]}',
    {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['list', 'add', 'remove', 'reorder'],
                'description': 'Action to perform (required)',
            },
            'symbol': {
                'type': 'string',
                'description': 'Asset symbol for add/remove (e.g. BTC, ETH)',
            },
            'symbols': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': (
                    'Full ordered symbol list for reorder action (max 200 items)'
                ),
            },
        },
        'required': ['action'],
    },
    min_tier='free',  # watchlist is available to all tiers in-app
)
def watchlist_manage(arguments):
    from user_db import user_db as _user_db

    user_id = get_mcp_user_id()
    action = (arguments.get('action') or '').lower()

    if action == 'list':
        symbols = _user_db.get_flat_watchlist(user_id)
        return {'symbols': symbols, 'count': len(symbols)}

    if action == 'add':
        symbol = (arguments.get('symbol') or '').strip().upper()
        if not symbol:
            raise ToolError('symbol is required for action=add')
        if len(symbol) > _MAX_SYMBOL_LEN:
            raise ToolError(f'symbol too long (max {_MAX_SYMBOL_LEN} chars)')
        current = _user_db.get_flat_watchlist(user_id)
        if symbol in current:
            # Adding an already-present symbol is a no-op — say so
            # instead of claiming "added" when nothing changed.
            return {'symbols': current, 'count': len(current), 'already_present': symbol}
        if len(current) >= _MAX_SYMBOLS:
            raise ToolError(f'Watchlist is full (max {_MAX_SYMBOLS} symbols)')
        ok = _user_db.add_to_watchlist(user_id, symbol)
        if not ok:
            raise ToolError(f'Failed to add {symbol} to watchlist')
        symbols = _user_db.get_flat_watchlist(user_id)
        return {'symbols': symbols, 'count': len(symbols), 'added': symbol}

    if action == 'remove':
        symbol = (arguments.get('symbol') or '').strip().upper()
        if not symbol:
            raise ToolError('symbol is required for action=remove')
        ok = _user_db.remove_from_watchlist(user_id, symbol)
        if not ok:
            raise ToolError(f'Failed to remove {symbol} from watchlist')
        symbols = _user_db.get_flat_watchlist(user_id)
        return {'symbols': symbols, 'count': len(symbols), 'removed': symbol}

    if action == 'reorder':
        raw_symbols = arguments.get('symbols')
        if not isinstance(raw_symbols, list):
            raise ToolError('symbols must be an array of strings for action=reorder')
        if len(raw_symbols) > _MAX_SYMBOLS:
            raise ToolError(f'Too many symbols (max {_MAX_SYMBOLS})')
        cleaned = []
        for s in raw_symbols:
            sym = str(s).strip().upper()
            if not sym:
                continue
            if len(sym) > _MAX_SYMBOL_LEN:
                raise ToolError(f'Symbol "{sym}" too long (max {_MAX_SYMBOL_LEN} chars)')
            if sym not in cleaned:  # de-dupe while preserving order
                cleaned.append(sym)
        ok = _user_db.save_flat_watchlist(user_id, cleaned)
        if not ok:
            raise ToolError('Failed to save reordered watchlist')
        symbols = _user_db.get_flat_watchlist(user_id)
        return {'symbols': symbols, 'count': len(symbols)}

    raise ToolError(f'Unknown action: {action!r}. Use list, add, remove, or reorder')
