# Configuration

Carrac is configured entirely through environment variables. This page lists
every one the application reads, grouped by feature, with the default the
code falls back to when the variable is unset.

The starting point is [`../.env.example`](../.env.example): copy it to `.env`
and fill in the four required values at the top. Setup steps are in
[`../README.md`](../README.md).

For everything below the required block, `./scripts/configure.sh` (also
`make configure`) asks feature by feature and writes the answers into
`.env`: it uncomments the key you give a value for, leaves the others alone,
and never prints a secret back. `--list` shows which optional keys are set;
`--set KEY=VALUE` sets one without questions.

## How configuration is loaded

**Under docker compose** (the normal way to run it):

- The `carrac`, `worker` and `migrate` services (and `carrac-b` in
  `docker-compose.yml`) load `.env` through `env_file:`.
- On top of that, both compose files set `DATABASE_URL`, `REDIS_URL`,
  `REDIS_SESSION_URL`, `DB_TYPE=postgresql` and `WORKER_MODE=true` in an
  `environment:` block, built from `POSTGRES_PASSWORD` and `REDIS_PASSWORD`.
  Those win over anything you put in `.env` for the same names.
- `.env` is not copied into the image (`.dockerignore` excludes it). The
  containers see only what compose passes in.
- Compose reads `.env` when it **creates** a container. `docker compose
  restart` reuses the old environment. After editing `.env`, run
  `docker compose up -d --force-recreate` (for the dev stack, add
  `-f docker-compose.dev.yml`).

**Without docker**, `carrac/config.py` reads `.env` from the current working
directory when it is first imported, so start the app from the repository
root. Variables already set in the real environment win, with one quirk: a
variable that is set but empty is replaced by the `.env` value. Set
`CARRAC_SKIP_DOTENV=1` (also `true` or `yes`) to skip the file entirely; the
test suite does, so a local `.env` can never change a test result.

Most values are read once, when the process starts. A few switches noted
below are read on every request, but the recreate rule above still applies
to anything that comes from `.env`.

### Empty is not the same as unset

`os.getenv("X", default)` returns `""` — not the default — when `X=` appears
with nothing after it. That is why every optional key in `.env.example` is
commented out. In particular, an empty numeric setting (`BREVO_SMTP_PORT=`,
`AI_INPUT_RATE=` and the like) makes the app fail at startup.

### On/off switches do not all parse the same way

| Pattern | Variables |
|---|---|
| `true` / `1` / `yes` means on | most switches (`ENABLE_*`, `COMING_SOON_MODE`, `REPORT_EDITORIAL_ENABLED`, `CCXT_DISABLED`, …) |
| only `true` means on (any other value is off) | `SESSION_COOKIE_SECURE`, `CSP_NONCE_MODE`, `DEBUG` |
| only `1` / `true` means on (not `yes`) | `HYPERLIQUID_WS_ENABLED`, `CCXT_WS_ENABLED` |
| only exactly `1` means on | `DESK_ENABLED`, `DETAILS_DRAWER_ENABLED`, `CHART_ASGI_ENABLED`, `SCREENSHOT_ASGI_ENABLED` |
| only exactly `0` means off | `GROUNDING_ENABLED`, `GROUNDING_VERIFY_ENABLED` |
| `0` / `false` / `no` / `off` means off | `SIGNUPS_ENABLED`, `SHARE_INTERACTIVE_ENABLED`, `CHART_SHARE_SERVER_RENDER`, `SCANNER_EVENT_RISK_CAP`, `LIQ_WS_ENABLED`, `CHART_WARM_ENABLED` |
| **any non-empty value means on**, including `0` and `false` | `ASSETS_PAGES_PUBLIC` |

Note the two `*_ASGI_ENABLED` switches: setting them to `true` turns them
**off**, because only the literal `1` counts.

## Minimum to run

| Variable | Why |
|---|---|
| `POSTGRES_PASSWORD` | compose builds `DATABASE_URL` from it |
| `REDIS_PASSWORD` | compose builds `REDIS_URL` / `REDIS_SESSION_URL` from it |
| `SECRET_KEY` | signs sessions and CSRF tokens |
| `ADMIN_EMAIL` | the address that becomes the admin account |
| `SESSION_COOKIE_SECURE=false` | only if you serve over plain HTTP (see below) |

`./scripts/quickstart.sh` generates all of these, including the last one,
into a fresh `.env`.

With just these, you get charts, indicators, drawings, plays, alerts, the
journal and the MCP server. Crypto prices come from public exchange APIs that
need no key.

## Turning on features

| Feature | Set |
|---|---|
| Login over plain HTTP (no TLS proxy) | `SESSION_COOKIE_SECURE=false` |
| Correct links in emails | `SITE_URL` |
| Server-side chart screenshots (journal, share images, MCP chart tool) | `SCREENSHOT_API_KEY` |
| Saving exchange API keys; encrypted Telegram tokens | `TELEGRAM_TOKEN_ENCRYPTION_KEY` |
| Macro panels | `FRED_API_KEY` |
| Live US stock and ETF ticks | `FINNHUB_API_KEY` |
| AI features | `ANTHROPIC_API_KEY` + `AI_MODEL` (or `AI_API_KEY` + `AI_BASE_URL` + `AI_MODEL`), then grant credits to users |
| Email verification and password reset for other users | `BREVO_SMTP_*`, `BREVO_FROM_EMAIL`, `SITE_URL` |
| Browser push notifications | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` |
| Alerts to the operator (you) | `NTFY_BASE` + `NTFY_CARRAC_TOPIC`, optionally `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` |
| Error tracking | `SENTRY_DSN` |
| Bot check on sign-up | `TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET_KEY` |
| Closed sign-up | `SIGNUPS_ENABLED=false` |
| AI market reports | the AI keys above + `SCREENSHOT_API_KEY`, then run the report engine yourself |

## Reference

### Core

| Variable | Default | What it does |
|---|---|---|
| `POSTGRES_PASSWORD` | — | Read by docker compose only. Database password; compose builds `DATABASE_URL` from it. |
| `REDIS_PASSWORD` | — | Read by docker compose only. Redis password; compose builds `REDIS_URL` and `REDIS_SESSION_URL` from it. |
| `SECRET_KEY` | random per process | Signs session cookies and CSRF tokens. When `DATABASE_URL` is set, startup is refused unless it is at least 16 characters with at least 8 distinct ones. Without `DATABASE_URL` a weak or missing key is silently replaced by a random one, so logins do not survive a restart. |
| `ADMIN_EMAIL` | empty | Registering with this address creates the admin account, which can log in without verifying its email. Production refuses to start if this is a placeholder (@example.com/.org/.net). Also used to resolve "the admin" for internal API calls. |
| `DATABASE_URL` | empty (compose sets it) | PostgreSQL connection string. Unset means SQLite files under `DATA_DIR` — only good for tests and poking at code. |
| `DB_TYPE` | `postgresql` if `DATABASE_URL` is set, else `sqlite` | Which database driver to use. |
| `REDIS_URL` | `redis://localhost:6379/0` in config; several modules treat unset as "no Redis" | Cache, rate limits, AI spend tracking, worker heartbeat. Without Redis, caching falls back to memory. |
| `REDIS_SESSION_URL` | `redis://localhost:6379/1` | Where login sessions are stored. Falls back to `REDIS_URL`. |
| `DATA_DIR` | `data` | Directory for on-disk caches, snapshots, screenshots and AI logs. Mounted as a volume in compose. |
| `CARRAC_PORT` | `5002` | Host port of the dev (quickstart) stack. Read by `scripts/quickstart.sh` and `docker-compose.dev.yml` only, not by the app: it sets the published port (`127.0.0.1:<port>`) and the address the script checks. Export it for the script and for any later `docker compose -f docker-compose.dev.yml` command. |
| `CARRAC_SKIP_DOTENV` | unset | `1`, `true` or `yes` stops `carrac/config.py` reading `.env` at import. Meant for tests and tools that must run only on the real environment. |
| `PORT` | `5000` | Port the app uses to call **itself** (screenshots). The image always listens on 5000, so leave it. |
| `DEBUG` | `false` | Flask debug mode. Never on in a reachable deployment. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `SLOW_REQUEST_THRESHOLD_S` | `2.0` | Requests slower than this many seconds are logged as a warning. |

### Sessions, security and public address

| Variable | Default | What it does |
|---|---|---|
| `SESSION_COOKIE_SECURE` | `true` | Marks the login cookie HTTPS-only and sends an HSTS header. Must be `false` when you serve over plain HTTP, or the cookie is not sent back and every login bounces. Keep `true` behind TLS. |
| `CSP_NONCE_MODE` | `true` | Strict Content-Security-Policy with per-request script nonces. `false` loosens it to `unsafe-inline`, as an escape hatch. |
| `SITE_URL` | `http://localhost:5002` | Your public address. Base for absolute links in emails (verification, password reset, account unlock, welcome), and the address the iPhone widget script is told to call. The default only suits local development, so set it in production. Also used for share, alert and MCP links (see next row). |
| `LANDING_HOSTS` | unset | Comma-separated hostnames that serve only the public landing site (for example `example.com,www.example.com`). A request to one of them for a sign-in page, or from a signed-in visitor, is redirected (302) to the same path on `SITE_URL`. Unset: nothing is redirected. |
| `PUBLIC_BASE_URL` | unset | Overrides `SITE_URL` for share, alert-webhook and MCP links only. With neither set, those links use the request's own address. |
| `SCREENSHOT_API_KEY` | empty | Shared secret the app sends to itself in the `X-Internal-Key` header: server-side chart screenshots for journal entries, share images and the MCP chart tool, background chart refresh, the report engine. Without it those features fail or skip. Anyone holding it has admin-level API access. |
| `TELEGRAM_TOKEN_ENCRYPTION_KEY` | empty | Fernet key that encrypts every stored secret: users' Telegram bot tokens, exchange API secrets and webhook signing secrets. Unset: Telegram tokens and webhook secrets are stored in plaintext (with a warning in the log) and saving exchange API keys is refused. |

### Sign-up and account pages

| Variable | Default | What it does |
|---|---|---|
| `SIGNUPS_ENABLED` | `true` | `false` closes public registration; `/register` then accepts invite codes only. Register your admin account first. |
| `TURNSTILE_SITE_KEY` | empty | Cloudflare Turnstile public key shown on the register form. |
| `TURNSTILE_SECRET_KEY` | empty | Turnstile server-side key. Without it the bot check is skipped. If Cloudflare cannot be reached, sign-up is let through. |
| `DISCORD_INVITE_URL` | empty | Community invite link shown in the app menu, About page and welcome email. Empty hides the link everywhere. |
| `LEGAL_ENTITY_NAME` | `Carrac` | Operator name shown on the terms, privacy and refund pages. |
| `LEGAL_ENTITY_FORM` | empty | Legal form (e.g. "private limited company"). Empty leaves the "registered in" sentence out. |
| `LEGAL_ENTITY_COUNTRY` | empty | Jurisdiction, shown with the form. |
| `LEGAL_ENTITY_ADDRESS` | an "address on file" line (hidden on the pages) | Address on the privacy and refund pages. |
| `LEGAL_ENTITY_REGISTRY_NUMBER` | empty | Optional company number. |
| `LEGAL_ENTITY_VAT_ID` | empty | Optional VAT number. |
| `LEGAL_ENTITY_CONTACT_EMAIL` | `SUPPORT_EMAIL` | Contact for legal correspondence. |
| `SUPPORT_EMAIL` | empty | Your support address, shown on the terms, privacy, refund and disclaimer pages and the marketing footer. Empty shows a visible "[operator: set SUPPORT_EMAIL]" placeholder, never someone else's address. |

The legal pages were written for the original operator. If you run a public
instance, set the `LEGAL_ENTITY_*` values and read the templates too.

### Mobile widgets

| Widget | Where its server address comes from |
|---|---|
| iPhone (Scriptable) | Nothing to build. `/widget/scriptable.js` and the setup download are served with the script pointed at `SITE_URL` (or the address the request came in on, if `SITE_URL` is unset). |
| Android (`android-widget/`) | Build setting `carracBaseUrl`, e.g. `https://carrac.example.com`. Put `carracBaseUrl=...` in `android-widget/local.properties` or `~/.gradle/gradle.properties`, or pass `-PcarracBaseUrl=...`. It sets both the data address and the update-download link. Unset, it falls back to `https://carrac.example.invalid`, which never resolves, so an unconfigured build cannot talk to anyone. |

### Email

Sent over SMTP with STARTTLS. The `BREVO_` prefix is historical: any SMTP
relay works. Email is skipped (and an error logged) unless both user and
password are set.

| Variable | Default | What it does |
|---|---|---|
| `BREVO_SMTP_HOST` | `smtp-relay.brevo.com` | SMTP server. |
| `BREVO_SMTP_PORT` | `587` | SMTP port. |
| `BREVO_SMTP_USER` | empty | SMTP login. |
| `BREVO_SMTP_PASS` | empty | SMTP password. `BREVO_SMTP_PASSWORD` is accepted as an alias. |
| `BREVO_FROM_EMAIL` | empty | Sender address; email is not sent until it is set. Use a domain you control. |
| `BREVO_FROM_NAME` | `Carrac` | Sender display name. |

### Market data

| Variable | Default | What it does |
|---|---|---|
| `FRED_API_KEY` | empty | St. Louis Fed macro data (yields, CPI and similar), used by the macro panels, sentiment and the economic calendar. Free key. |
| `FINNHUB_API_KEY` | empty | Worker streams live US stock and ETF ticks into Redis. Free tier works. |
| `HYPERLIQUID_WS_ENABLED` | off | Worker keeps Hyperliquid coins' hourly bars fresh over WebSocket. |
| `HYPERLIQUID_DISABLED` | off | Kill switch: never use Hyperliquid as a data source. |
| `HL_SMART_MONEY_WALLETS` | empty | Comma-separated Hyperliquid wallet addresses for the smart-money positioning collector to use when the public leaderboard is unavailable. Empty means that day's run is skipped with a warning. |
| `CCXT_WS_ENABLED` | off | Worker streams prices for coins not listed on Binance (Gate.io, MEXC, KuCoin, Bybit via ccxt). |
| `CCXT_DISABLED` | off | Kill switch for all of those exchanges. |
| `CCXT_GATEIO_DISABLED`, `CCXT_MEXC_DISABLED`, `CCXT_KUCOIN_DISABLED`, `CCXT_BYBIT_DISABLED` | off | Kill switch for one exchange. The name is built at runtime as `CCXT_<EXCHANGE>_DISABLED`. |
| `LIQ_WS_ENABLED` | on | Worker listens to Binance's public forced-liquidation stream. |
| `TRON_PRO_API_KEY` | empty | Optional key for the stablecoin on-chain collector. Works without it at low volume. |
| `QUANT_DATABASE_URL` | falls back to `DATABASE_URL` | Database holding the quant (macro) tables, if you keep them separate. |

### AI

AI calls are charged against each user's credit balance, so a new user can do
nothing with AI until an admin grants credits from the admin user page. The
in-app chat is shown to admins; for everyone else it also needs a PostHog
feature flag (see "Original-operator integrations").

| Variable | Default | What it does |
|---|---|---|
| `AI_PROVIDER` | auto | `anthropic` or `openai` (any OpenAI-compatible endpoint). Unset: `anthropic` if `AI_MODEL` starts with `claude-`, otherwise `openai`. |
| `AI_MODEL` | `moonshotai/Kimi-K2.5` in config; `claude-opus-4-6` on the Anthropic path when unset | Model name. Note the default model name does not match the default `AI_BASE_URL` — set the two together. |
| `ANTHROPIC_API_KEY` | empty | Anthropic key. Falls back to `AI_API_KEY`. |
| `AI_API_KEY` | empty | Key for the OpenAI-compatible endpoint. Falls back to `TOGETHER_API_KEY`, then `XAI_API_KEY`. |
| `AI_BASE_URL` | `https://api.x.ai/v1/chat/completions` | Full chat-completions URL for the OpenAI-compatible provider. |
| `TOGETHER_API_KEY`, `TOGETHER_MODEL` | empty | Older names for `AI_API_KEY` and `AI_MODEL`, still read. |
| `XAI_API_KEY` | empty | xAI key. Fallback for `AI_API_KEY`; the report engine also uses it for X search and voice. |
| `X_SEARCH_HANDLES` | empty | Comma-separated X handles the report engine restricts its Grok `x_search` to. Empty disables `x_search` for the macro research step (logged as a warning); also the suggested list in the analyst-sources screen. |
| `AI_GLOBAL_DAILY_USD_CAP` | `30` | Ceiling on raw AI spend per UTC day across all users, in USD, tracked in Redis. Past it, in-app chat and the AI weekly review are refused until 00:00 UTC. The journal AI features count toward the total but do not check it. |
| `CREDIT_MARKUP` | `3.0` | Multiplier from raw provider cost to the credits a user is charged. `1.0` charges at cost. |
| `AI_INPUT_RATE`, `AI_OUTPUT_RATE` | `16.0`, `80.0` | USD per million input / output tokens, used to cost OpenAI-compatible calls. Anthropic calls use a built-in per-model price table; models missing from it are costed at Opus rates. |
| `AI_ESTIMATE_CENTS` | `25` | Credits held before an AI call when the caller gives no estimate; settled to the real cost afterwards. |
| `AI_CONSERVATIVE_ESTIMATE_CENTS` | `25` | Credits held before an in-app chat call. |
| `AI_JOURNAL_REVIEW_ESTIMATE`, `AI_SESSION_SUMMARY_ESTIMATE`, `AI_WEEKLY_REVIEW_ESTIMATE`, `AI_NL_QUERY_ESTIMATE` | `25`, `25`, `35`, `20` | Credits held before each journal AI feature. |
| `AI_COST_TAX_MULTIPLIER` | `1.27` | Factor applied to costs in the admin per-user economics view (tax on API credit purchases). Display only. |
| `AI_LOG_DIR` | `DATA_DIR/ai-logs` | Where each AI call's prompt and response are logged as JSONL for admin review. |
| `GROUNDING_ENABLED` | on | Adds a block of current facts (prices, rates, dates) to AI prompts and scans output for invented numbers. `0` turns both off. |
| `GROUNDING_VERIFY_ENABLED` | on | Second-pass fact check of generated text against its source. `0` turns it off. |
| `GROUNDING_VERIFY_MODEL` | `claude-haiku-4-5` | Model for that fact check. |
| `DASHBOARD_BASE_URL` | `http://localhost:5000` | Where the grounding facts block and the report engine fetch live prices from (the app itself). |
| `DASHBOARD_API_KEY` | empty | Internal key the grounding facts block sends with those fetches. Set it to the same value as `SCREENSHOT_API_KEY`. |

### AI market reports (report engine)

The report engine writes the daily market reports. It is a separate
command, run inside the app container, and nothing in the repository
schedules it for you:

```bash
docker compose exec carrac python -m morning_engine --mode morning
```

Other modes are `afternoon`, `weekly`, `dispatch` and `week_ahead`. It needs
an Anthropic key and `SCREENSHOT_API_KEY`.

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-opus-4-6` | Larger model, used when `MORNING_PRIMARY_MODEL=opus`. |
| `ANTHROPIC_MODEL_FAST` | `claude-sonnet-4-6` | Default report model. |
| `MORNING_PRIMARY_MODEL` | unset (Sonnet) | `opus` or `grok` to switch the report model. |
| `ENABLE_GENERAL_REPORT` | on | Generate the shared report everyone sees. |
| `ENABLE_MORNING_REPORT` | on | Generate the operator's own report (with the admin's positions and plays). Off also hides the latest-report audio endpoint. With both this and `ENABLE_GENERAL_REPORT` off, the public report pages say reports are paused. |
| `PERSONALIZED_REPORTS` | off | Per-user personalised reports. |
| `GENERAL_REPORT_UNGATED` | off | Produce the shared report even in regions with no eligible users. |
| `ENABLE_CRITIC` | on | Run a review pass over each shared report before publishing. |
| `TTS_ENABLED` | on | Produce an audio version of reports (xAI voice). |
| `TTS_VOICE_ID` | `rex` | Voice for that audio. |
| `OPENAI_API_KEY` | empty | Fallback text-to-speech through OpenAI when set. |
| `SENTRY_ENV` | `production` | Environment tag on the report engine's error reports. |
| `APP_VERSION` | `unknown` | Release tag on the report engine's error reports. |

### Notifications

| Variable | Default | What it does |
|---|---|---|
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` | empty | Web-push key pair. Generate with `npx web-push generate-vapid-keys`. |
| `VAPID_SUBJECT` | `mailto:admin@example.com` | Contact address push services require. |
| `NTFY_BASE_HOST` | empty | Users may always send their alerts to `ntfy.sh`; this allows one more ntfy host (yours). |
| `NTFY_BASE` | empty | ntfy server for **operator** alerts (data-source outages, guard alarms). Unset: those alerts are dropped, with one error logged per process. |
| `NTFY_CARRAC_TOPIC` | empty | Topic for operator alerts. |
| `NTFY_MARKET_TOPIC` | empty | Topic for market report digests. |
| `NTFY_TOPIC` | empty | Older single topic. Only the quant data collectors still post their failures here. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Operator Telegram chat: urgent alerts are mirrored here and the report engine can deliver reports. Users' own Telegram alerts use tokens they enter in settings. |
| `DELIVERY_OUTAGE_WINDOW_HOURS` | `24` | Window for detecting that a user's alert channel keeps failing. |
| `DELIVERY_OUTAGE_MIN_FAILURES` | `3` | Failures within that window before the outage alarm fires. |

### Error tracking

| Variable | Default | What it does |
|---|---|---|
| `SENTRY_DSN` | empty | Any Sentry-compatible endpoint (Sentry, Bugsink, GlitchTip). Unset: the SDK is never initialised. |
| `SENTRY_BROWSER_DSN` | empty | Public DSN for browser error reports. Unset: the browser SDK is not loaded and `/api/error-tunnel` answers 404. Set: reports go through the tunnel to the host and project of `SENTRY_DSN`; the tunnel rejects any envelope that does not name exactly this DSN. Only user id and tier are attached, never email. |

### Feature switches

| Variable | Default | What it does |
|---|---|---|
| `COMING_SOON_MODE` | off | Anonymous visitors to `/` get a holding page instead of the landing page. |
| `DESK_ENABLED` | off | Opens the Trader's Desk (`/desk`) to everyone and makes it the post-login page. Off: admins only. Needs exactly `1`. |
| `DETAILS_DRAWER_ENABLED` | off | Per-asset details drawer on the screener. Needs exactly `1`. |
| `ASSETS_PAGES_PUBLIC` | off | Public `/assets` pages and their sitemap entries. **Any** non-empty value turns it on. |
| `REPORT_EDITORIAL_ENABLED` | off | Editorial report layout for everyone. Off: admins can preview it with `?editorial=1`. |
| `DERIVATIVES_DESK_ENABLED` | off | Derivatives section in reports for everyone. Admins always see it. |
| `SHARE_INTERACTIVE_ENABLED` | on | Interactive chart viewer on shared links. Off falls back to a static image. |
| `CHART_SHARE_SERVER_RENDER` | on | Render share images on the server (needs `SCREENSHOT_API_KEY`). |
| `CLOUDFLARE_WEB_ANALYTICS` | off | Set to `1` when Cloudflare Web Analytics is on for your zone: the Content-Security-Policy then allows the beacon Cloudflare injects at the edge. Off: the browser blocks it. |
| `WHOAMI_DEBUG` | off | Enables the `/_whoami` debugging endpoint. |
| `CURL_RENDER_BIN` | `./bin/carrac-render` | Path to the terminal chart renderer behind the plain-text (curl) chart endpoint. The Docker image downloads it from a pinned, checksummed [carrac-tui](https://github.com/carrac-app/carrac-tui) release (`scripts/fetch-tui-renderer.py`); outside Docker, install carrac-tui and point this at its `carrac` binary. |

### Routing kill switches

These exist to roll back a migration step, not to tune anything. Leave them
at their defaults.

| Variable | Default | What it does |
|---|---|---|
| `CHART_ASGI_ENABLED` | `1` | Serve `GET /api/chart/<symbol>` through the FastAPI front door. Anything other than `1` sends it back to Flask. |
| `SCREENSHOT_ASGI_ENABLED` | `1` | Same for the screenshot and headless-chart routes. Flask-direct screenshots are known to stall, so this is not a safe fallback. |

### Performance tuning

| Variable | Default | What it does |
|---|---|---|
| `PRECOMPUTE_WORKERS` | `1` | Threads for the worker's per-symbol precompute pass. Measured no faster on a warm cache. |
| `LTF_HOT_TIER` | `BTC,ETH,SOL,XRP,DOGE,ADA,AVAX,LINK,DOT,BNB,BTC.D,TOTAL` | Symbols whose short timeframes are kept warm. |
| `CHART_WARM_ENABLED` | on | Worker pre-builds popular chart responses each cycle. Read live. |
| `CHART_WARM_BUDGET_S` | `45` | Time limit per chart-warming cycle, in seconds. |
| `BACKFILL_INTERVAL_SECONDS` | `300` | Pause between history-backfill fetches. |
| `STALE_ALARM_JUMP` | `3` | Alarm when at least this many symbols go stale in one cycle (e.g. a delisting batch). |
| `SENTIMENT_COMPUTE_BUDGET_S` | `12` | Time limit for computing sentiment during a request. |
| `SENTIMENT_BACKGROUND_BUDGET_S` | `45` | Time limit for the background refresh of sentiment. |
| `SENTIMENT_FRED_SUBBUDGET_FRACTION` | `1.0` | Share of that budget the FRED-based parts may use. |
| `LIQ_SPIKE_MIN_USD` | `250000` | Minimum liquidations (USD) before the scanner calls it a spike. |
| `LIQ_SPIKE_RATIO` | `3.0` | How far above its recent hourly rate liquidations must run to count as a spike. |
| `SCANNER_EVENT_RISK_HOURS` | `36` | Look-ahead window for scheduled events in the scanner. |
| `SCANNER_EVENT_RISK_CAP` | on | Caps a STRONG scanner verdict when such an event falls inside the window. |
| `SCREENSHOT_MAX_CONCURRENCY` | `2` | Concurrent headless-browser renders per web worker process. |
| `SCREENSHOT_QUEUE_TIMEOUT` | `30` | Seconds a screenshot request waits for a free render slot. |

### Database migrations (one-off)

Set these only for a single run of the `migrate` service
(`scripts/alembic_stamp_if_fresh.py`), then remove them.

| Variable | Default | What it does |
|---|---|---|
| `ALEMBIC_ADOPT` | off | Let the migration step adopt an existing database that has tables but no migration history. Without it the step refuses, so an old backup cannot silently skip migrations. |
| `SKIP_QUANT_CHECK` | off | Skip the check that the quant tables are up to date. |

### Payments (never switched on)

Paddle billing code is in the repository, but payments were never turned on
in the original deployment. With `PADDLE_API_KEY` and `PADDLE_CLIENT_TOKEN`
unset, the upgrade and checkout UI stays hidden. Leave this group unset.

| Variable | Default | What it does |
|---|---|---|
| `PADDLE_API_KEY` | empty | Paddle server API key. |
| `PADDLE_CLIENT_TOKEN` | empty | Paddle.js client token for the checkout overlay. |
| `PADDLE_WEBHOOK_SECRET` | empty | Verifies Paddle webhooks. |
| `PADDLE_ENVIRONMENT` | `sandbox` | `sandbox` or `production`. |
| `PADDLE_PRICE_MEMBER`, `PADDLE_PRICE_PREMIUM`, `PADDLE_PRICE_TOPUP_25`, `PADDLE_PRICE_TOPUP_50`, `PADDLE_PRICE_TOPUP_100` | empty | Map Paddle prices to tiers and credit top-ups. Leave empty unless you run Paddle billing; empty IDs are ignored, so payments stay off. |

### Original-operator integrations

These connected the original deployment to services it ran alongside. They
work only with those services and are safe to ignore.

| Variable | Default | What it does |
|---|---|---|
| `POSTHOG_API_KEY` | empty | PostHog product analytics: adds the browser snippet, and server-side feature flags (`signups_paused`, `ai_chat_enabled`). |
| `POSTHOG_HOST` | `https://eu.i.posthog.com` | PostHog region. |
| `BETA_END_TS` | `2026-08-31T23:59:59+00:00` | Sign-ups before this time are tagged as the beta cohort in analytics. |
| `ESPOCRM_URL`, `ESPOCRM_API_KEY` | empty | CRM sync. Retired; does nothing when unset. |
| `POSTIZ_URL`, `POSTIZ_API_KEY` | empty | Social-post scheduler for draft posts. |
| `POSTIZ_API_PATH` | `/api/public/v1` | Its API path. |
| `POSTIZ_X_INTEGRATION_ID` | empty | Which connected X account to post to. |
| `SOCIAL_DRY_RUN` | dry run | Anything except the literal `0` keeps social posting in dry-run mode. |
| `SOCIAL_LINK_URL` | your `SITE_URL` | Link attached to generated posts. |

### Set by the stack — do not set by hand

| Variable | What it does |
|---|---|
| `WORKER_MODE` | Set to `true` by compose on every app container (and forced by `worker.py`). It keeps background jobs out of the web containers so they run once, in the worker. |
| `TESTING` | Set by the test suite; skips background threads. |

### Read but unused

`carrac/config.py` still reads `SCREENSHOT_SERVICE_URL`,
`SCREENSHOT_SERVICE_KEY`, `COINGLASS_API_KEY`, `WEBHOOK_BASE_URL`,
`AI_PROVIDER_INPUT_RATE` and `AI_PROVIDER_OUTPUT_RATE`, but nothing uses the
values. Setting them has no effect.

## Not covered here

- **Python SDK.** `CARRAC_API_KEY` and `CARRAC_BASE_URL` (older names
  `CONVEXITY_API_KEY` and `CONVEXITY_BASE_URL`) configure the client in
  [`../sdk-python/`](../sdk-python/README.md), not the server.
- **Maintenance scripts** under `scripts/` read a few variables of their own
  that the app never reads: `AI_LOG_RETENTION_DAYS` (AI log pruning),
  `CARRAC_DATA_DIR`, `CARRAC_CACHE_BASELINE` and `CARRAC_CACHE_STATE` (cache
  integrity check), `IDLE_IN_TX_THRESHOLD` (database probe),
  `BROWSER_SMOKE_BASE`, `SMOKE_LOGIN_EMAIL`, `SMOKE_LOGIN_PASSWORD`,
  `SMOKE_ADMIN_EMAIL` and `SMOKE_ADMIN_PASSWORD` (browser smoke test), and
  `DUR` and `LOAD_ABORT` (load test). Several of these scripts were written
  for the original production host and assume its paths and cron setup; read
  each one before you run it.
- Two variables read by a report-rendering helper under `docs/` are left out.
