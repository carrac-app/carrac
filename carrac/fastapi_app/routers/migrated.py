"""Routes flipped from Flask to FastAPI by the strangler dispatcher.

Each handler here was ported verbatim from Flask. The match is checked
in tests via ``tests/fastapi/test_public_routes.py`` (the sitemap is
compared with ``tests/fastapi/baselines/sitemap.xml``, captured from the
Flask handler output).

Public read endpoints (no auth):

* ``GET /health`` — Postgres + Redis + cache + worker-heartbeat probe.
* ``GET /robots.txt`` — full marketing-allow block; byte-identical to
  the Flask version in carrac/dashboard/routes.py.
* ``GET /sitemap.xml`` — full marketing-page sitemap; byte-identical
  to the Flask version in carrac/dashboard/routes.py.

The Flask handlers in ``carrac/dashboard/routes.py`` remain in place
for a parity period per the FastAPI migration plan
("renamed to ``_flask_legacy`` and kept passing for a short period as
parity comparison; then deleted").
"""

from __future__ import annotations

from carrac.site_url import site_base_url
import json as _json
import time as _time
from datetime import datetime as _dt

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from changelog import APP_VERSION

router = APIRouter()


@router.get("/health", include_in_schema=False)
def health() -> JSONResponse:
    """Liveness probe — 200 if the ASGI worker is alive enough to dispatch
    a request. No DB, no Redis, no compute. Targeted at Docker's
    ``healthcheck`` directive (fires every 30s with a 10s budget).

    For readiness — DB + Redis + cache + worker heartbeat — see
    ``/health/ready`` below. External monitors (Kuma, the uptime watchdog)
    should probe that route, not this one.

    ``version`` is purely additive — ``status``
    stays exactly ``"alive"`` since docker healthchecks, ``promote``'s
    health-wait loop and the smoke tests all key off it. ``version``
    lets ``scripts/smoke-test.sh`` assert the running container is
    actually built from the working tree's code, not just that *some*
    process answered on the port.
    """
    return JSONResponse({"status": "alive", "version": APP_VERSION}, status_code=200)


@router.get("/health/ready", include_in_schema=False)
def health_ready() -> JSONResponse:
    """Readiness probe — verifies Postgres, Redis, response cache, and the
    background worker heartbeat. 200 healthy / 503 degraded.

    Ported verbatim from the original ``/health`` body so the response
    shape matches byte-for-byte (modulo ``response_time_ms``, which is
    naturally volatile). The cheap liveness ``/health`` route lives
    above; this route is what Kuma and the uptime watchdog target.
    """
    start = _time.monotonic()
    checks: dict = {}
    degraded = False

    # PostgreSQL connectivity
    try:
        from carrac.db_pool import get_conn, get_db_type

        db_type = get_db_type()
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        checks["database"] = {"status": "ok", "type": db_type}
    except Exception as e:  # noqa: BLE001 — mirror Flask
        checks["database"] = {"status": "error", "error": str(e)[:100]}
        degraded = True

    # Redis ping
    try:
        from carrac.redis_cache import get_redis

        r = get_redis()
        if r:
            r.ping()
            checks["redis"] = {"status": "ok"}
        else:
            checks["redis"] = {"status": "unavailable"}
            degraded = True
    except Exception as e:  # noqa: BLE001
        checks["redis"] = {"status": "error", "error": str(e)[:100]}
        degraded = True

    # Response cache freshness
    try:
        from carrac.redis_cache import ResponseCache

        cache = ResponseCache()
        stats = cache.get_stats()
        htf_fresh = stats.get("htf", {}).get("fresh", False)
        htf_age = stats.get("htf", {}).get("age_seconds", 0) or 0
        checks["cache"] = {
            "htf": stats.get("htf", {}),
            "ltf": stats.get("ltf", {}),
        }
        # Only mark degraded if cache is very stale (>20min) — brief gaps
        # between precompute cycles are normal and the app serves fresh
        # data on-demand.
        if not htf_fresh and htf_age > 1200:
            degraded = True
    except Exception:  # noqa: BLE001
        checks["cache"] = {"status": "unknown"}

    # Worker heartbeat (surfaced for smoke tests + uptime
    # monitors). Stale > 600 s marks degraded; so does an ABSENT key.
    #
    # Absence must NOT read as healthy: that inverts the alarm for the only
    # case that matters. The key carries a TTL (900 s from precompute, 300 s
    # from worker.py), so a dead worker stops refreshing it and it EXPIRES;
    # age=None would then report healthy and the outage would clear its own
    # alarm. Also, when worker.py wrote last the 300 s TTL means age can never
    # reach the 600 s threshold. This is the one endpoint Kuma watches.
    #
    # Absence is safe to treat as degraded because Redis is not recreated by a
    # promote (`up -d --no-deps carrac worker`), so the key survives deploys and
    # is refreshed roughly every 80 s. It is missing only after ~15 min of a
    # dead worker — or on a first-ever boot against an empty Redis, where the
    # Redis check above already reports degraded anyway.
    worker_age_sec: int | None = None
    worker_seen = False
    try:
        from carrac.caches import _get_redis

        _r = _get_redis()
        if _r:
            raw = _r.get("mcd:worker:heartbeat")
            if raw:
                worker_seen = True
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                payload = _json.loads(raw)
                ts = _dt.fromisoformat(payload["ts"])
                worker_age_sec = int((_dt.now() - ts).total_seconds())
    except Exception:  # noqa: BLE001
        pass

    degraded_reasons: list[str] = []
    if worker_age_sec is not None and worker_age_sec > 600:
        degraded = True
        degraded_reasons.append("worker heartbeat stale")
    elif not worker_seen:
        degraded = True
        degraded_reasons.append("worker heartbeat absent")

    elapsed_ms = int((_time.monotonic() - start) * 1000)
    status = "degraded" if degraded else "healthy"
    code = 503 if degraded else 200

    return JSONResponse(
        {
            "status": status,
            "checks": checks,
            "worker_heartbeat_age_sec": worker_age_sec,
            "degraded_reasons": degraded_reasons,
            "response_time_ms": elapsed_ms,
        },
        status_code=code,
    )


@router.get("/robots.txt", include_in_schema=False)
def robots_txt() -> PlainTextResponse:
    # Access: AI *search/answer* agents are explicitly
    # welcome — being read and cited in an AI answer is a distribution channel.
    # AI *training* crawlers (GPTBot, ClaudeBot, CCBot, Google-Extended, …) are
    # disallowed by a Cloudflare-managed block that is PREPENDED to this body at
    # the edge; it is not in this file and cannot be edited here. This group
    # states the search-side intent explicitly so the policy survives future
    # changes to that managed list.
    content = (
        "User-agent: OAI-SearchBot\n"
        "User-agent: ChatGPT-User\n"
        "User-agent: PerplexityBot\n"
        "User-agent: Perplexity-User\n"
        "User-agent: Claude-SearchBot\n"
        "User-agent: Claude-User\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /admin\n"
        "Disallow: /settings\n"
        "Disallow: /chart/\n"
        "Disallow: /report/\n"
        "Disallow: /chart-headless/\n"
        "\n"
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /indicators\n"
        "Allow: /changelog\n"
        "Allow: /formulas\n"
        "Allow: /guide\n"
        "Allow: /terms\n"
        "Allow: /privacy\n"
        "Allow: /disclaimer\n"
        "Allow: /contact\n"
        "Disallow: /api/\n"
        "Disallow: /admin\n"
        "Disallow: /settings\n"
        "Disallow: /chart/\n"
        "Disallow: /report/\n"
        "Disallow: /chart-headless/\n"
        "\n"
        f"Sitemap: {site_base_url()}/sitemap.xml\n"
    )
    return PlainTextResponse(content)


@router.get("/llms.txt", include_in_schema=False)
def llms_txt() -> PlainTextResponse:
    """``/llms.txt`` — llmstxt.org curated entry point for AI agents.

    Complements robots.txt/sitemap.xml: robots says what
    a crawler *may* fetch, the sitemap says what *exists*, this says what is
    worth reading and how to connect an agent directly over MCP.

    Grounding rule: every claim here must be checkable on the linked page. No
    performance numbers, and no advice framing — see the non-advice posture in
    ``carrac://grounding/rules``.

    The tool counts are read from the live registry, not typed
    in. They had drifted stale here and on four marketing pages at once.
    """
    try:
        import carrac.mcp.tools  # noqa: F401 — triggers @mcp_tool registration
        from carrac.mcp.registry import TOOL_REGISTRY

        _n_tools = len(TOOL_REGISTRY)
        _n_free = sum(1 for m in TOOL_REGISTRY.values() if m.get('min_tier') == 'free')
    except Exception:  # never fail the page over a count
        _n_tools = _n_free = 'many'

    content = """# Carrac

> Market intelligence for humans and AI. Charts, a key-level scanner and
> twice-daily written market research for crypto, equities, metals, energy and
> agricultural markets — all readable by an AI agent over MCP (Model Context
> Protocol). Information and analysis for evaluation; not financial advice, and
> no trade recommendations are published.

Carrac is built to be read by agents as well as people. The same market data,
scanner output and research that render in the web app are exposed as MCP tools,
so an assistant can read them directly instead of scraping screenshots.

## For AI agents

- [MCP server]({_base}/mcp): how to connect an agent. {_n_tools} tools; {_n_free}
  usable on the free tier (market data, glossary, general report, read-only
  charts). Write actions and deep quant series require a signed-in account.
- [Tool + workflow guide]({_base}/guide): tool catalogue and
  copy-paste examples.
- [Indicator reference]({_base}/indicators): definitions for every
  indicator the charts and scanner use.
- [Formula DSL]({_base}/formulas): the expression grammar for custom
  indicators and conditions.

## Research (updated daily)

- [Latest market brief]({_base}/brief): the current written market
  note.
- [Brief archive]({_base}/brief/archive): dated permalinks to past
  briefs.
- [Market regime]({_base}/market-regime): how regime classification
  is defined and applied.

## Per-asset pages

- [All assets]({_base}/assets): index of the public asset pages.
- Examples: [BTC]({_base}/assets/BTC),
  [ETH]({_base}/assets/ETH), [SPX]({_base}/assets/SPX),
  [GOLD]({_base}/assets/GOLD), [OIL]({_base}/assets/OIL),
  [WHEAT]({_base}/assets/WHEAT).

## Product

- [How the analysis is built]({_base}/intelligence)
- [Markets covered]({_base}/markets)
- [Integrations]({_base}/integrations)
- [Changelog]({_base}/changelog)

## Optional

- [Terms]({_base}/terms)
- [Privacy]({_base}/privacy)
- [Disclaimer]({_base}/disclaimer) — Carrac publishes information and
  analysis, not personal financial advice or trade recommendations.
- [Contact]({_base}/contact)
"""
    content = (content
               .replace('{_n_tools}', str(_n_tools))
               .replace('{_n_free}', str(_n_free))
               .replace('{_base}', site_base_url()))
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


def _list_recent_briefs_for_sitemap():
    """``[{date, tldr}]`` for the last N dated /brief permalinks — never
    raises (degrades to ``[]``), so a DB hiccup can never break the sitemap."""
    try:
        from carrac.reports.brief_archive import list_recent_general_reports
        return list_recent_general_reports()
    except Exception:
        return []


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml() -> Response:
    base = site_base_url()
    pages = [
        {"loc": "/", "priority": "1.0", "changefreq": "weekly", "lastmod": "2026-06-02"},
        # Tier 2.1 — public daily brief (flagship top-of-funnel page, updates daily).
        {"loc": "/brief", "priority": "0.9", "changefreq": "daily", "lastmod": "2026-07-02"},
        # Permalink archive index. The dated
        # /brief/<date> pages themselves are deliberately NOT enumerated
        # here yet: doing so would need this sitemap builder to query the
        # shared reports DB singleton, which isn't reset between tests and
        # would make this baseline-diffed route flaky (the archive page only is
        # added; the dated sitemap is a follow-up).
        {"loc": "/brief/archive", "priority": "0.7", "changefreq": "daily", "lastmod": "2026-07-02"},
        # Dated-sitemap follow-up — reuses list_recent_general_reports
        # (already bounded + cheap point-lookups) so each dated permalink is
        # discoverable. A dated permalink's content is immutable once published,
        # hence changefreq "never".
        *(
            {"loc": f"/brief/{_r['date']}", "priority": "0.6", "changefreq": "never", "lastmod": _r['date']}
            for _r in _list_recent_briefs_for_sitemap()
        ),
        {"loc": "/mcp", "priority": "0.8", "changefreq": "monthly", "lastmod": "2026-06-03"},
        {"loc": "/market-regime", "priority": "0.8", "changefreq": "monthly", "lastmod": "2026-06-04"},
        {"loc": "/intelligence", "priority": "0.8", "changefreq": "monthly", "lastmod": "2026-06-02"},
        {"loc": "/workflow", "priority": "0.8", "changefreq": "monthly", "lastmod": "2026-06-02"},
        {"loc": "/integrations", "priority": "0.8", "changefreq": "monthly", "lastmod": "2026-06-02"},
        {"loc": "/markets", "priority": "0.7", "changefreq": "weekly", "lastmod": "2026-06-02"},
        {"loc": "/indicators", "priority": "0.7", "changefreq": "monthly", "lastmod": "2026-06-02"},
        {"loc": "/formulas", "priority": "0.7", "changefreq": "monthly", "lastmod": "2026-06-02"},
        {"loc": "/guide", "priority": "0.8", "changefreq": "monthly", "lastmod": "2026-06-02"},
        {"loc": "/changelog", "priority": "0.5", "changefreq": "weekly", "lastmod": "2026-06-02"},
        {"loc": "/contact", "priority": "0.4", "changefreq": "yearly", "lastmod": "2026-06-02"},
        {"loc": "/terms", "priority": "0.3", "changefreq": "yearly", "lastmod": "2026-06-02"},
        {"loc": "/privacy", "priority": "0.3", "changefreq": "yearly", "lastmod": "2026-06-02"},
        {"loc": "/disclaimer", "priority": "0.3", "changefreq": "yearly", "lastmod": "2026-06-02"},
    ]
    # Public /assets directory + per-asset permalinks — only listed once the
    # surface is flipped public (env ASSETS_PAGES_PUBLIC); dark otherwise.
    import os
    if os.environ.get("ASSETS_PAGES_PUBLIC"):
        from carrac.assets.public_pages import allowed_symbols
        pages.append({"loc": "/assets", "priority": "0.7", "changefreq": "weekly", "lastmod": "2026-06-29"})
        for _s in allowed_symbols():
            pages.append({"loc": f"/assets/{_s}", "priority": "0.6", "changefreq": "daily", "lastmod": "2026-06-29"})
    entries = []
    for p in pages:
        entries.append(
            "  <url>\n"
            f"    <loc>{base}{p['loc']}</loc>\n"
            f"    <lastmod>{p['lastmod']}</lastmod>\n"
            f"    <changefreq>{p['changefreq']}</changefreq>\n"
            f"    <priority>{p['priority']}</priority>\n"
            "  </url>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>"
    )
    return Response(xml, media_type="application/xml")
