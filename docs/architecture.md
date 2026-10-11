# Architecture

How Carrac fits together, as of v0.226.22.

## Overview

One Python codebase, one Docker image, run in two roles: the **web app** serves
pages, the API and the MCP server; the **worker** does everything a request
should not wait for. Postgres is the system of record and Redis carries
sessions, the shared cache and rate limits. Scheduled jobs (the daily brief and
the quant collectors) run from cron against the same image.

| Container | Role |
|---|---|
| `carrac` | web app: gunicorn with Uvicorn workers, entry point `carrac.asgi:asgi_app` |
| `carrac-worker` | `python worker.py`: cache warming, backfill, live feeds, schedulers |
| `carrac-postgres` | PostgreSQL 16 |
| `carrac-redis` | Redis 7, password-protected |
| `carrac-migrate` | one-off: applies the Alembic migrations before the app starts |

`docker compose --profile ops` adds a second web container for rolling
restarts behind a load balancer, and `autoheal`, which restarts any container
whose healthcheck fails.

## Request routing

The web app serves **Flask and FastAPI from one process**. An ASGI dispatcher
([`carrac/asgi.py`](../carrac/asgi.py)) routes each request by path and method
from explicit tables: async-heavy and high-traffic routes go to FastAPI, the
remaining routes to Flask. Both read the same Redis-backed session, so one login is valid
everywhere, and both share one rate-limit budget per client.
`tests/test_strangler_guard.py` checks every Flask route against the
dispatcher tables, so a table edit cannot silently shadow a handler.

## One chart load

1. The browser requests `GET /api/chart/BTC?timeframe=1D`.
2. FastAPI resolves the user or API key and applies the rate limit without
   blocking the event loop.
3. The chart cache answers. A fresh payload is returned directly. A stale one
   still in cache is served at once with `X-Carrac-Stale` and `X-Carrac-Age`
   headers while a rebuild runs in the background, so clients can display data
   freshness. Freshness limits are set per timeframe.
4. Only a cold miss reads the on-disk bars, fetches the missing tail from the
   provider and computes the indicators.

The worker's `chartwarm` cycle proactively warms a bounded set of core symbols
and watchlist charts, which keeps common chart requests on the cache path.

## Caching

| Layer | Lifetime | Purpose |
|---|---|---|
| in-process | seconds | the hottest keys, without a Redis round-trip |
| Redis (`carrac/caches.py`) | 2–60 min per namespace | shared across processes and containers, with a local fallback |
| disk | minutes to hours | raw OHLCV bars, delta-fetched |

## The worker

The web containers start no background threads; the worker owns everything
that must happen whether or not a browser is open:

- the precompute loop: scanner scoring, alerts, transitions, plays and chart
  warming
- OHLCV history backfill
- live WebSocket feeds (Binance liquidations, Hyperliquid, CCXT, Finnhub), each
  switched on independently and isolated so one failure cannot stop the others
- schedulers for sentiment, liquidation heatmaps, exchange sync and data
  retention
- a Redis heartbeat and an orderly shutdown on SIGTERM

## Scheduled jobs

`python -m morning_engine` writes the daily, afternoon, weekly and week-ahead
briefs; `python -m quant.collectors.<name>` gathers COT, FRED macro, Fear and
Greed, ETF flows, stablecoin supply, Bitcoin network data, funding, Google
Trends and options data. [`deploy/crontab.example`](../deploy/crontab.example)
has the schedule.

A brief is built in stages: deterministic fact assembly, signal extraction, a
language model (Anthropic, Grok or any OpenAI-compatible endpoint), then four
independent checks before delivery: prompt rules, regex tripwires for advice
and unsupported figures, a publish gate, and a second model that verifies each
claim against the source facts.

## Data sources

`data_sources/` holds one client per provider on a shared base with a circuit
breaker, a fallback chain and health tracking. Price sources are normalised
into one candle format.

Each provider’s terms decide how its data may be used. In short:

| Source | Terms |
|---|---|
| Binance, Hyperliquid, public Ethereum and Tron nodes | market data and public facts; usable |
| CoinPaprika (free tier) | non-commercial, attribution required |
| Yahoo Finance chart API, Farside, Google Trends | no explicit reuse grant |
| mempool.space | no grant and no prohibition; low volume only |
| Deribit | collected, shown to admins only: its terms bar redistribution |

Check the current terms of each provider before any commercial use.

## Storage

**PostgreSQL** holds users, API keys, drawings, plays, alerts, push
subscriptions, the journal, backtest runs and the `quant` schema. Migrations
are Alembic, single head. `tests/schema/carrac_pg_schema.sql` is a dump of the
production schema; `scripts/check_schema_drift.py` confirms that the
migrations rebuild it exactly.

**Redis** carries sessions, the shared cache, rate-limit windows and usage
counters. **Files** under `data/` hold only the OHLCV cache and backfill
progress.

The test suite runs on SQLite for speed; the `tests/pg/` suite runs the write
paths against PostgreSQL in CI.

## Frontend

Vanilla JavaScript with no build step, Jinja templates, and KLineChart on
canvas. `static/js/indicator-registry.js` declares all 77 indicators once;
every menu, mobile sheet, group editor and toggle map is generated from it. A
versioned service worker makes the app installable and keeps a cached copy for
offline use.

## Design highlights

- **A formula language instead of plugins** (`carrac/formula_eval.py`). User
  conditions for alerts, plays and backtests are parsed and evaluated by a
  small purpose-built language: no `eval`, a whitelist of functions, and hard
  limits on length, calls and run time. Python and JavaScript evaluators are
  held to identical answers by parity tests.
- **One classification, two evaluators.** Alerts and plays share
  `carrac/indicator_eval.py`, and parity tests assert they agree on every
  indicator state.
- **Tiers defined once** (`carrac/tiers.py`). The web routes and the MCP
  tools read the same entitlement table.
- **Freshness is part of the response.** Cached payloads carry their own age,
  so speed never hides staleness.

## Security

- Server-side sessions in Redis; cookies `Secure`, `HttpOnly`, `SameSite=Lax`
- Passwords hashed with Argon2
- API keys hashed at rest and compared in constant time, scoped `full` or
  `read-only`; cross-user engine endpoints accept only the admin key
- CSRF tokens on form routes; API-key requests carry no cookie to forge
- Redis-backed rate limits, per key then per IP
- Nonce-based Content Security Policy
- Exactly one trusted proxy hop
- Containers run as a non-root user

`X-Internal-Key` is the app’s internal credential header. Behind a reverse
proxy, strip it from every request that does not come from loopback:

```nginx
# http {} level: keep X-Internal-Key only when the caller is on loopback.
map $remote_addr $internal_key {
    default    "";
    127.0.0.1  $http_x_internal_key;
    ::1        $http_x_internal_key;
}

server {
    # ... listen, server_name, TLS ...
    location / {
        proxy_set_header X-Internal-Key $internal_key;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_pass http://127.0.0.1:5002;
    }
}
```

## Deploying changes

Code is baked into the image, so a change goes live only when the image is
rebuilt: `docker compose build` then `docker compose up -d --force-recreate`.
The `migrate` service runs first. `docker restart` re-runs the existing image.

Every setting is listed in [configuration.md](configuration.md).
