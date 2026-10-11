# @carrac_cc/sdk

A small TypeScript client for the Carrac REST API. Works anywhere `fetch`
does: Node 18 or newer, Deno, Bun and modern browsers.

## Point it at your own instance

There is no default host: you must tell the SDK which Carrac server to talk
to. Run your own instance (see the [main README](../README.md)) and pass its
address as `baseUrl`:

```ts
import { Client } from "@carrac_cc/sdk";

const carrac = new Client({
  apiKey: process.env.CARRAC_API_KEY!,
  baseUrl: "http://localhost:5002",
});
```

or set `CARRAC_BASE_URL` in the environment (Node and other runtimes that
expose `process.env`). Without either, the constructor throws.
`http://localhost:5002` is the address the repository's Docker Compose setup
and `scripts/quickstart.sh` publish the app on. The API key is not read from
the environment; pass it explicitly.

**0.2.0 (breaking):** the built-in default of `https://app.carrac.cc` was
removed because the hosted service is being retired. Existing code that relied
on it must now pass `baseUrl` or set `CARRAC_BASE_URL`.

## Install

From this directory (matches the source you are reading):

```bash
npm ci && npm run build      # produces dist/
npm install /path/to/carrac/sdk-typescript   # from your own project
```

An `@carrac_cc/sdk` package was published to npm at 0.1.0. This source is
0.2.0, so the published package may lag it.

## Quick start

```ts
// Market data
const chart = await carrac.charts.get("BTC", { timeframe: "4h", indicators: "CPR,RSI" });

// Composite call
const detail = await carrac.composite.assetDetail("BTC", { format: "compact" });

// Create a price alert (condition is "above" or "below"; the server rejects fields it does not know)
await carrac.alerts.create({ symbol: "BTC", condition: "above", price: 120000 });

// Your usage
const usage = await carrac.usage.get();
// Limited accounts list one entry per window, e.g.
// [{window:"minute", used:7, limit:60, remaining:53}, ...]. An unlimited
// account, such as the admin the quickstart creates, logs [].
console.log(usage.rest.usage);
```

Alerts need a delivery channel first: an instance refuses `alerts.create` with
a 400 (`no_channel`) until one exists. Set one in the web app under **Settings →
Notifications**, or with one call (`saveNotificationSettings` in the API reference):

```bash
curl -X POST "$CARRAC_BASE_URL/api/notification-settings" \
  -H "X-API-Key: $CARRAC_API_KEY" -H "Content-Type: application/json" \
  -d '{"ntfy": {"enabled": true, "url": "https://ntfy.sh/your-topic"}}'
```

## Authentication

Sign in to your instance, open **Settings → API & Developer**, click
**Create Key** and copy the key (it is shown once). Pass it as `apiKey`.

Without the UI, `POST /api/keys` with a signed-in session, a JSON body such as
`{"name": "sdk", "scope": "full"}` and an `X-CSRFToken` header returns the key.
The token is the `content` of the `<meta name="csrf-token">` tag on any
signed-in page; without it the request fails with `400 CSRF validation failed`.

```bash
BASE=http://localhost:5002; JAR=$(mktemp)
field() { grep -oE "$1" | head -1 | sed -E 's/.*(value|content)="([^"]*)".*/\2/'; }

# Sign in: the login form carries its own CSRF token.
T=$(curl -s -c $JAR -b $JAR $BASE/login | field 'name="csrf_token" value="[^"]*"')
curl -s -o /dev/null -c $JAR -b $JAR $BASE/login \
  --data-urlencode "email=you@example.com" --data-urlencode "password=YOUR_PASSWORD" \
  --data-urlencode "csrf_token=$T"

# Any signed-in page carries the token JSON requests need.
CSRF=$(curl -s -c $JAR -b $JAR $BASE/settings | field '<meta name="csrf-token" content="[^"]*"')
curl -s -c $JAR -b $JAR -X POST $BASE/api/keys \
  -H 'Content-Type: application/json' -H "X-CSRFToken: $CSRF" \
  -d '{"name": "sdk", "scope": "full"}'
```

## Behaviour

- Retries up to `maxRetries` times (default 3): a `429` waits for the
  server's `Retry-After`, a `5xx` backs off exponentially from one second.
- Ships ESM with a CommonJS fallback.
- Keys are scoped to one user and rate-limited by that account's limits.

## Development

```bash
npm ci
npm run build
npm test
```

## API reference

A running instance serves its own interactive reference at `/api/docs`
and the OpenAPI document at `/api/openapi.json` — for the local setup,
http://localhost:5002/api/docs.

## Licence

MIT — see [LICENSE](LICENSE). The SDK is MIT so it can be embedded in
anything; the Carrac application itself is AGPL-3.0.
