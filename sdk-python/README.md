# Carrac Python SDK

A small Python client for the Carrac REST API: market data, plays, alerts,
the scanner and your own API usage.

## Point it at your own instance

There is no default host: you must tell the SDK which Carrac server to talk
to. Run your own instance (see the [main README](../README.md)) and pass its
address:

```python
from carrac import Client

carrac = Client(api_key="cvx_k_...", base_url="http://localhost:5002")
```

or set `CARRAC_BASE_URL` in the environment. Without either, `Client(...)`
raises `ValueError`. `http://localhost:5002` is the address the repository's
Docker Compose setup and `scripts/quickstart.sh` publish the app on.

**0.2.0 (breaking):** the built-in default of `https://app.carrac.cc` was
removed because the hosted service is being retired. Existing code that relied
on it must now pass `base_url` or set `CARRAC_BASE_URL`.

## Install

Install the source you are reading into its own virtual environment, from the
repository root:

```bash
python3 -m venv .venv-sdk && .venv-sdk/bin/pip install ./sdk-python
```

Run your code from outside the repository checkout: the repo's own `carrac/`
package would shadow the SDK.

A `carrac-sdk` package was published to PyPI at 0.1.0. This source is
0.2.0, so the published package may lag it.

Requires Python 3.9 or newer; the only dependency is `httpx`.

Do not install the SDK into the app's own virtualenv: both are named `carrac`,
so the app's `carrac/` package and the SDK's module shadow each other. Use a
separate virtualenv (above), or a directory outside the Carrac checkout.

## Quick start

```python
from carrac import Client

carrac = Client(api_key="cvx_k_...", base_url="http://localhost:5002")

# Market data
chart = carrac.charts.get("BTC", timeframe="4h", indicators="CPR,RSI")

# Composite call — everything about an asset in one request
detail = carrac.composite.asset_detail("BTC", format="compact")

# Create a price alert (condition is "above" or "below"; other fields are note=,
# frequency= ("once" or "once_per_bar"), journal_id= and auto_close= ("tp_hit"
# or "sl_hit"); the server rejects any field it does not know)
carrac.alerts.create(symbol="BTC", condition="above", price=120000)

# Scanner
ranked = carrac.scanner.list(direction="long", verdict="STRONG,GOOD")

# Your usage
usage = carrac.usage.get()
# Limited accounts list one entry per window, e.g.
# [{window:"minute", used:7, limit:60, remaining:53}, ...]. An unlimited
# account, such as the admin the quickstart creates, prints [].
print(usage["rest"]["usage"])
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

1. Sign in to your instance and open **Settings → API & Developer**.
2. Click **Create Key** and copy the full key (it is shown once).
3. Pass it as `Client(api_key=...)` or set `CARRAC_API_KEY` in the environment.

Without the UI, `POST /api/keys` with a signed-in session, a JSON body such
as `{"name": "sdk", "scope": "full"}` (`scope` is `full` or `read-only`) and an
`X-CSRFToken` header returns the key once. The token is the `content` of the
`<meta name="csrf-token">` tag on any signed-in page; without it the request
fails with `400 CSRF validation failed`:

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

- Retries up to `max_retries` times (default 3): a `429` waits for the
  server's `Retry-After`, a `5xx` backs off exponentially from one second.
- Keys are scoped to one user and rate-limited by that account's limits.

## API reference

A running instance serves its own interactive reference at `/api/docs`
and the OpenAPI document at `/api/openapi.json` — for the local setup,
http://localhost:5002/api/docs.

## Licence

MIT — see [LICENSE](LICENSE). The SDK is MIT so it can be embedded in
anything; the Carrac application itself is AGPL-3.0.
