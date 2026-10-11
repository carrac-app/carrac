"""Static-ish text surfaces for the curl endpoint: footer, help, assets, overview."""
from __future__ import annotations

from carrac.site_url import site_base_url, site_host

DIM = "\x1b[2m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
RESET = "\x1b[0m"

OVERVIEW_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "LINK"]

# Register funnel URLs. Everything a human READS or TYPES is a PURE URL —
# nobody types query strings. utm tags ride only the invisible surfaces: the
# browser 302 Locations (REGISTER_URL / REGISTER_TUI_URL) and the QR payload.
# Typed-entry attribution comes from the short path itself (/reg, /reg-tui).
# Keep these in sync with the /reg + /reg-tui handlers + tests.
def register_display() -> str:
    return f"{site_host()}/reg"


def register_url() -> str:
    return f"{site_base_url()}/register?utm_source=curl&utm_medium=terminal"


def register_tui_url() -> str:
    return f"{site_base_url()}/register?utm_source=tui&utm_medium=terminal"


def register_qr_url() -> str:
    return f"{site_base_url()}/register?utm_source=curl-qr"


# MCP connect config — mirror the canonical block in templates/guide.html
# (#mcp). Endpoint + X-API-Key header shape must match exactly; do not invent.
def mcp_endpoint() -> str:
    return f"{site_base_url()}/mcp"


def funnel_footer(plain: bool) -> str:
    h = site_host()
    line = (
        f"live + interactive → {h}"
        f" · MCP for your AI agent → {h}/mcp"
        f" · register → {h}/reg"
    )
    return f"{line}\n" if plain else f"{DIM}{line}{RESET}\n"


def unknown_symbol(symbol: str, plain: bool) -> str:
    msg = f"'{symbol}' isn't tracked (yet) — tracked assets: curl {site_host()}/:assets\n"
    return msg if plain else f"{RED}{msg}{RESET}"


def usage_error(detail: str, plain: bool) -> str:
    h = site_host()
    msg = f"{detail}\nusage: curl {h}/btc@10d?ema — full help: curl {h}/:help\n"
    return msg if plain else f"{RED}{msg}{RESET}"


# plain accepted for signature uniformity; message carries no ANSI either way.
def rate_limited(plain: bool) -> str:
    return (
        "easy there — rate limited. charts refresh every 60s anyway.\n"
        f"unlimited + interactive: {site_host()}\n"
    )


def help_page() -> str:
    h = site_host()
    return f"""\
{h} — terminal market charts

usage:
    curl {h}/btc            chart, last 1 day
    curl {h}/btc@10d        @window: 1h 4h 1d 3d 7d 10d 30d 90d 1y
    curl {h}/spx@90d        stocks + indices too
    curl {h}/:assets        everything we track

options (combinable, rate.sx style):
    ?ema          EMA ribbon overlay
    ?T            plain text, no colors
    ?q            quiet — chart only
    ?cols=120     width: 80 100 120 160
    e.g.          curl '{h}/eth@30d?qT&cols=120'

data refreshes every 60 seconds. for information only — not investment advice.
full platform (alerts, plays, scanner, AI, MCP): {h}
"""


def assets_page(symbols: list[str]) -> str:
    cols = 8
    syms = sorted(s.lower() for s in symbols)
    rows = []
    for i in range(0, len(syms), cols):
        rows.append("  ".join(f"{s:<8}" for s in syms[i : i + cols]))
    return "tracked assets:\n" + "\n".join(rows) + f"\n\nchart one: curl {site_host()}/<symbol>\n"


def _prices() -> dict:
    from cache_manager import get_cached_crypto_prices

    return get_cached_crypto_prices()


def overview_table(plain: bool) -> str:
    prices = _prices()
    lines = ["  #  asset   price        24h"]
    rank = 0
    for sym in OVERVIEW_SYMBOLS:
        p = prices.get(sym)
        if not p:
            continue
        rank += 1
        chg = p.get("change_pct", 0.0) or 0.0
        chg_s = f"{chg:.2f}%"
        if not plain:
            color = GREEN if chg >= 0 else RED
            chg_s = f"{color}{chg_s}{RESET}"
        price = p.get("price") or 0
        lines.append(f"  {rank:<2} {sym:<7} {price:<12g} {chg_s}")
    lines.append("")
    lines.append(f"chart anything:  curl {site_host()}/btc@10d   (stocks too: /spx, /gold)")
    lines.append(f"help:            curl {site_host()}/:help")
    return "\n".join(lines) + "\n"


def register_page(plain: bool) -> str:
    """Terminal register funnel: 2–3 lines of copy + ANSI QR + a PURE URL.

    In plain mode (``?T``) the QR is skipped and only the URL is printed.
    The displayed URL is pure (typed on any device, it re-enters /reg where
    the browser branch attaches attribution invisibly); the QR encodes the
    ``curl-qr`` source so phone scans are tracked separately.
    """
    head = (
        "create your free Carrac account — alerts, plays, scanner, AI + MCP.\n"
        "scan the code or open the link on any device:\n"
    )
    if plain:
        return f"{head}\n{register_display()}\n"
    from carrac.curl_charts.qr import render_qr

    return f"{head}\n{render_qr(register_qr_url())}\n{register_display()}\n"


def mcp_page() -> str:
    """Terminal-native MCP onboarding with a ready-to-paste client config.

    Mirrors the canonical connect block in templates/guide.html (#mcp):
    endpoint ``POST <site>/mcp`` + ``X-API-Key`` header.
    """
    return f"""\
carrac MCP — give your AI agent live market data + your trades

connect any MCP-compatible client (Claude Code, Claude Desktop, Cursor,
Windsurf, custom agents). Speaks JSON-RPC 2.0 over HTTP (Streamable HTTP).

    endpoint:  POST {mcp_endpoint()}
    auth:      X-API-Key header (your account API key)

Claude Code (~/.claude/settings.json):
{{
  "mcpServers": {{
    "carrac": {{
      "type": "url",
      "url": "{mcp_endpoint()}",
      "headers": {{
        "X-API-Key": "your-api-key-here"
      }}
    }}
  }}
}}

Claude Desktop (claude_desktop_config.json):
{{
  "mcpServers": {{
    "carrac": {{
      "command": "npx",
      "args": ["-y", "mcp-remote",
        "{mcp_endpoint()}",
        "--header", "X-API-Key:your-api-key-here"]
    }}
  }}
}}

MCP needs an account — the free tier includes the core tools.
get your API key: curl {site_host()}/reg
"""
