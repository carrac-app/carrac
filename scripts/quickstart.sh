#!/usr/bin/env bash
# Clone-to-login quickstart.
#
# One mode: docker compose with Postgres (docker-compose.dev.yml). Zero API
# keys, zero questions. Generates .env with real secrets, brings up the dev
# stack, waits for it, then runs the "stranger smoke" — register, log in,
# render a chart, save a drawing, create + evaluate a play, list MCP tools —
# printing each line as it passes. Re-run any time; it never overwrites an
# existing .env.
#
# Usage:
#   ./scripts/quickstart.sh            start the stack and run the smoke
#   ./scripts/quickstart.sh --check    skip `compose up`, smoke a running stack
#   ./scripts/quickstart.sh --verbose  show the image build output instead of
#                                      writing it to quickstart-build.log
#   CARRAC_PORT=5099 ./scripts/quickstart.sh   publish on another host port (default 5002)
#
# Needs: docker (with the `compose` v2 plugin), curl, openssl. Nothing else —
# no host python, no jq, no other infrastructure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILE="docker-compose.dev.yml"
ENV_FILE=".env"
# Host port the app is published on (docker-compose.dev.yml reads the same variable).
CARRAC_PORT="${CARRAC_PORT:-5002}"
export CARRAC_PORT
BASE_URL="http://localhost:${CARRAC_PORT}"
HEALTH_TIMEOUT=300
READY_TIMEOUT="${READY_TIMEOUT:-900}"
CHECK_ONLY=0
VERBOSE=0
BUILD_LOG="$REPO_ROOT/quickstart-build.log"

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -v|--verbose) VERBOSE=1 ;;
    -h|--help)
      sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (supported: --check, --verbose)" >&2
      exit 1
      ;;
  esac
done

COOKIE_JAR="$(mktemp)"
TMP_BODY="$(mktemp)"
cleanup() { rm -f "$COOKIE_JAR" "$TMP_BODY"; }
trap cleanup EXIT

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# Colour only when the stream is a terminal, so piped or logged output is clean.
GREEN="" RED="" OUT_RESET="" ERR_RESET=""
if [[ -t 1 ]]; then GREEN=$'\033[32m' OUT_RESET=$'\033[0m'; fi
if [[ -t 2 ]]; then RED=$'\033[31m' ERR_RESET=$'\033[0m'; fi
say_pass() { printf '  %s\xE2\x9C\x93%s %s\n' "$GREEN" "$OUT_RESET" "$1"; }
say_fail() { printf '  %s\xE2\x9C\x97%s %s\n' "$RED" "$ERR_RESET" "$1" >&2; }

# Every preflight failure names the fix, not the symptom.
preflight_fail() {
  say_fail "$1"
  echo "  fix: $2" >&2
  exit 1
}

# Run a command with its output appended to $BUILD_LOG; on failure show the
# tail of the log and where the rest is.
quiet_step() {
  local msg="$1"
  shift
  if ! "$@" >> "$BUILD_LOG" 2>&1; then
    say_fail "$msg"
    echo "── last 30 lines of $BUILD_LOG ──" >&2
    tail -n 30 "$BUILD_LOG" >&2
    exit 1
  fi
}

fail_with_logs() {
  say_fail "$1"
  echo >&2
  echo "── last 30 lines of \`docker compose -f $COMPOSE_FILE logs carrac\` ──" >&2
  compose logs --tail=30 carrac >&2 2>&1 || true
  exit 1
}

# ---------------------------------------------------------------------------
# HTTP helpers — one curl call per request, body to a shared tmpfile so JSON
# and multi-line HTML are both handled without any string-splitting tricks.
# ---------------------------------------------------------------------------

http() {
  # http METHOD URL [extra curl args...] — prints the status code, leaves
  # the response body in $TMP_BODY.
  #
  # A non-default User-Agent matters here: carrac/asgi.py rewrites a bare
  # GET / to a plain-text terminal overview for any UA matching
  # curl/|wget/|HTTPie/|... (the curl-endpoint dispatcher) instead of
  # serving index.html — which is where the post-login CSRF <meta> tag
  # lives. curl's default "curl/X.Y.Z" UA trips that, so every request
  # here carries one that doesn't.
  local method="$1" url="$2"
  shift 2
  curl -sS -A 'carrac-quickstart/1.0' -b "$COOKIE_JAR" -c "$COOKIE_JAR" -X "$method" -o "$TMP_BODY" -w '%{http_code}' "$@" "$url"
}

# Note: every extractor below ends in `|| true`. Under `set -o pipefail`, a
# grep that matches nothing exits 1, which — for `var="$(fn)"` — is a plain
# command whose failure `set -e` acts on immediately, aborting the whole
# script with no message before the caller's own `[[ -z "$var" ]]` check
# ever runs. `|| true` lets "not found" surface as an empty string that the
# caller can diagnose properly instead of a silent, unexplained exit.

# CSRF token from a register.html / login.html hidden form field.
csrf_from_body() {
  grep -oE 'name="csrf_token" value="[^"]*"' "$TMP_BODY" | head -1 | sed -E 's/.*value="([^"]*)".*/\1/' || true
}

# CSRF token from the <meta name="csrf-token"> tag templates render for a
# logged-in page (index.html etc.) — a different spot than the login/register
# forms, so a different pattern.
csrf_from_meta() {
  grep -oE '<meta name="csrf-token" content="[^"]*"' "$TMP_BODY" | head -1 | sed -E 's/.*content="([^"]*)".*/\1/' || true
}

# JSON is parsed by the stdlib json module, never with grep: the API's spacing
# is not a contract ("count": 1 vs "count":1). The interpreter that runs it is
# the one inside the app container, so the host needs no python. Tests replace
# json_python with a host interpreter.
json_python() { compose exec -T carrac python "$@"; }

# json_field KEY [len] — reads $TMP_BODY. Prints the top-level KEY (dotted
# paths descend into objects) when it is a string or number; with `len`, the
# length of a list. Prints nothing when the key is absent or the wrong shape.
JSON_FIELD_PY='
import json, sys
try:
    node = json.load(sys.stdin)
    for part in sys.argv[1].split("."):
        node = node[part]
except (ValueError, KeyError, TypeError):
    sys.exit(0)
if len(sys.argv) > 2 and sys.argv[2] == "len":
    if isinstance(node, list):
        print(len(node))
elif isinstance(node, (str, int)) and not isinstance(node, bool):
    print(node)
'
json_field() {
  json_python -c "$JSON_FIELD_PY" "$@" < "$TMP_BODY" || true
}

# The first top-level `<div class="error-message">…</div>` in the current
# $TMP_BODY (register.html / login.html re-render an error this way on a
# non-redirect response). Flatten to one line first — login.html's error div
# wraps across several lines.
extract_error_text() {
  tr '\n' ' ' < "$TMP_BODY" \
    | grep -oE '<div class="error-message"[^>]*>[^<]*' \
    | head -1 \
    | sed -E 's/<div class="error-message"[^>]*>[[:space:]]*//; s/[[:space:]]+$//' \
    || true
}

# A one-line hint at what page $TMP_BODY actually is, for a token-extraction
# failure to report instead of just "not found" — e.g. "you got the
# curl-endpoint terminal overview, not index.html" is diagnosable; a bare
# "no CSRF token" is not. Prefers <title>; falls back to the first 120 bytes
# for pages (like the plain-text terminal overview) that have none.
page_hint() {
  local title
  title="$(grep -oE '<title>[^<]*</title>' "$TMP_BODY" | head -1 | sed -E 's#</?title>##g' || true)"
  if [[ -n "$title" ]]; then
    echo "title: $title"
  else
    echo "first 120 bytes: $(head -c 120 "$TMP_BODY" | tr '\n' ' ')"
  fi
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

preflight() {
  command -v docker >/dev/null 2>&1 || preflight_fail \
    "docker not found" \
    "install Docker — https://docs.docker.com/get-docker/"

  docker info >/dev/null 2>&1 || preflight_fail \
    "docker daemon not reachable" \
    "start Docker (e.g. \`sudo systemctl start docker\`, or open Docker Desktop)"

  docker compose version >/dev/null 2>&1 || preflight_fail \
    "docker compose (v2) not found" \
    "install the compose plugin — https://docs.docker.com/compose/install/"

  command -v curl >/dev/null 2>&1 || preflight_fail \
    "curl not found" \
    "install curl with your package manager (apt/dnf/brew install curl)"

  command -v openssl >/dev/null 2>&1 || preflight_fail \
    "openssl not found" \
    "install openssl with your package manager (apt/dnf/brew install openssl)"

  local port_busy=""
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":${CARRAC_PORT} " && port_busy=1
  elif command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$CARRAC_PORT" >/dev/null 2>&1 && port_busy=1
  else
    echo "  (no \`ss\` or \`nc\` on this host — skipping the port-${CARRAC_PORT} check)"
  fi
  if [[ -n "$port_busy" ]]; then
    preflight_fail \
      "port ${CARRAC_PORT} is already in use" \
      "stop whatever is listening on 127.0.0.1:${CARRAC_PORT} (another Carrac stack? \`docker compose -f $COMPOSE_FILE down\`?), then re-run, or pick another port with CARRAC_PORT=<port>"
  fi

  if command -v df >/dev/null 2>&1; then
    local free_kb
    free_kb="$(df -Pk . | awk 'NR==2{print $4}')"
    if [[ -n "$free_kb" && "$free_kb" -lt 4194304 ]]; then
      preflight_fail \
        "less than 4 GB free on this filesystem ($(( free_kb / 1024 )) MB free)" \
        "free up space for the Postgres/Redis volumes and image layers, then re-run"
    fi
  fi
}

# ---------------------------------------------------------------------------
# .env — generate once with real secrets, never overwrite an existing one.
# ---------------------------------------------------------------------------

# A login password that clears security.py's validate_password(): >=1
# upper, >=1 lower, >=1 digit, >=1 special, no char repeated 4+ times, and
# no 3-char sequential run of digits or letters (e.g. "234", "hij") anywhere
# — including across the body/marker boundary, so the whole candidate is
# re-checked each loop, not just the random part.
generate_password() {
  local body pw ok
  while true; do
    body="$(openssl rand -base64 30 | tr -dc 'A-Za-z0-9' | cut -c1-16)"
    pw="${body}-Qs!"
    ok=1
    grep -qE '[A-Z]' <<<"$pw" || ok=0
    grep -qE '[a-z]' <<<"$pw" || ok=0
    grep -qE '[0-9]' <<<"$pw" || ok=0
    grep -qE '(.)\1{3,}' <<<"$pw" && ok=0
    grep -qE '(012|123|234|345|456|567|678|789|890)' <<<"$pw" && ok=0
    grep -qiE '(abc|bcd|cde|def|efg|fgh|ghi|hij|ijk|jkl|klm|lmn|mno|nop|opq|pqr|qrs|rst|stu|tuv|uvw|vwx|wxy|xyz)' <<<"$pw" && ok=0
    if [[ $ok -eq 1 ]]; then
      echo "$pw"
      return
    fi
  done
}

generate_env() {
  if [[ -f "$ENV_FILE" ]]; then
    echo "Using existing $ENV_FILE"
    ADMIN_EMAIL="$(grep -E '^ADMIN_EMAIL=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
    QUICKSTART_PASSWORD="$(grep -E '^QUICKSTART_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
    if [[ -z "$ADMIN_EMAIL" ]]; then
      preflight_fail \
        "$ENV_FILE exists but has no ADMIN_EMAIL" \
        "set ADMIN_EMAIL=you@example.com in $ENV_FILE, then re-run"
    fi
    if [[ -z "$QUICKSTART_PASSWORD" ]]; then
      QUICKSTART_PASSWORD="$(generate_password)"
      {
        echo
        echo "# Generated by scripts/quickstart.sh — the login password for ADMIN_EMAIL."
        echo "QUICKSTART_PASSWORD=$QUICKSTART_PASSWORD"
      } >> "$ENV_FILE"
    fi
  else
    echo "Generating $ENV_FILE from .env.example"
    cp .env.example "$ENV_FILE"
    ADMIN_EMAIL="admin@carrac.local"
    QUICKSTART_PASSWORD="$(generate_password)"
    local postgres_password redis_password secret_key internal_key fernet_key
    postgres_password="$(openssl rand -hex 24)"
    redis_password="$(openssl rand -hex 24)"
    secret_key="$(openssl rand -hex 24)"
    internal_key="$(openssl rand -hex 32)"
    # A Fernet key is 32 random bytes in URL-safe base64.
    fernet_key="$(openssl rand -base64 32 | tr '+/' '-_')"
    sed -i.bak \
      -e "s#^POSTGRES_PASSWORD=.*#POSTGRES_PASSWORD=$postgres_password#" \
      -e "s#^REDIS_PASSWORD=.*#REDIS_PASSWORD=$redis_password#" \
      -e "s#^SECRET_KEY=.*#SECRET_KEY=$secret_key#" \
      -e "s#^ADMIN_EMAIL=.*#ADMIN_EMAIL=$ADMIN_EMAIL#" \
      "$ENV_FILE"
    rm -f "$ENV_FILE.bak"
    {
      echo
      echo "# Generated by scripts/quickstart.sh — the login password for ADMIN_EMAIL."
      echo "# Read back (never regenerated) on every re-run, so"
      echo "# ./scripts/quickstart.sh --check prints the same login."
      echo "QUICKSTART_PASSWORD=$QUICKSTART_PASSWORD"
      echo
      echo "# Generated by scripts/quickstart.sh — see the internal-key and at-rest"
      echo "# encryption notes in .env.example."
      echo "SCREENSHOT_API_KEY=$internal_key"
      echo "TELEGRAM_TOKEN_ENCRYPTION_KEY=$fernet_key"
    } >> "$ENV_FILE"
  fi

  # The dev stack serves plain HTTP on 127.0.0.1 — a Secure-flagged session
  # cookie never comes back from a browser or curl without HTTPS, which
  # breaks login outright. docker-compose.yml (prod, behind TLS-terminating
  # nginx) is untouched by this — the override lives only in the generated
  # .env, never in .env.example.
  if ! grep -q '^SESSION_COOKIE_SECURE=' "$ENV_FILE"; then
    echo "SESSION_COOKIE_SECURE=false" >> "$ENV_FILE"
  fi
}

# ---------------------------------------------------------------------------
# Waits
# ---------------------------------------------------------------------------

wait_for_health() {
  printf 'Waiting for the app to answer /health'
  local waited=0
  until curl -sf -o /dev/null "$BASE_URL/health" 2>/dev/null; do
    if [[ $waited -ge $HEALTH_TIMEOUT ]]; then
      echo
      fail_with_logs "app never answered /health within ${HEALTH_TIMEOUT}s"
    fi
    printf '.'
    sleep 2
    waited=$((waited + 2))
  done
  echo
}

# Tell the user once that the first boot is slow because the worker is filling
# the market cache.
WARMING_NOTED=0
note_warming() {
  if [[ $WARMING_NOTED -eq 0 ]]; then
    echo "  (worker is warming the market cache, first boot only — this can take several minutes)"
    WARMING_NOTED=1
  fi
}

wait_for_ready() {
  local start=$SECONDS
  while true; do
    local status
    status="$(http GET "$BASE_URL/health/ready")"
    if [[ "$status" == "200" ]]; then
      say_pass "app answers /health/ready"
      return
    fi
    if (( SECONDS - start >= READY_TIMEOUT )); then
      fail_with_logs "app never became ready within ${READY_TIMEOUT}s (last /health/ready: HTTP $status, $(cat "$TMP_BODY" | head -c 300))"
    fi
    note_warming
    printf '.'
    sleep 5
  done
}

# ---------------------------------------------------------------------------
# Stranger smoke
# ---------------------------------------------------------------------------

register_and_login() {
  local status csrf

  # Register. A fresh clone creates the admin account; a re-run against an
  # account that already exists re-renders the form with an error instead of
  # redirecting — that's fine, we go straight to login either way.
  http GET "$BASE_URL/register" >/dev/null
  csrf="$(csrf_from_body)"
  if [[ -z "$csrf" ]]; then
    fail_with_logs "no CSRF token found on GET /register — is the app serving the expected page? ($(page_hint))"
  fi
  status="$(http POST "$BASE_URL/register" \
    --data-urlencode "email=$ADMIN_EMAIL" \
    --data-urlencode "password=$QUICKSTART_PASSWORD" \
    --data-urlencode "password_confirm=$QUICKSTART_PASSWORD" \
    --data-urlencode "display_name=Quickstart" \
    --data-urlencode "agree_terms=on" \
    --data-urlencode "age_attested=on" \
    --data-urlencode "csrf_token=$csrf")"
  if [[ "$status" == "302" ]]; then
    : # new account created
  elif [[ "$status" == "200" ]]; then
    local reg_err
    reg_err="$(extract_error_text)"
    # This exact text (carrac/auth/routes.py) means the address is already
    # registered — expected on a re-run, fine to continue to login. Any
    # other error (e.g. a password-policy rejection) is a real failure.
    if [[ "$reg_err" != "Registration failed. Please check your details and try again." ]]; then
      fail_with_logs "register rejected: ${reg_err:-<no error text found>} (HTTP $status)"
    fi
  else
    fail_with_logs "register failed (HTTP $status): $(cat "$TMP_BODY" | head -c 300)"
  fi

  # Login.
  http GET "$BASE_URL/login" >/dev/null
  csrf="$(csrf_from_body)"
  if [[ -z "$csrf" ]]; then
    fail_with_logs "no CSRF token found on GET /login — is the app serving the expected page? ($(page_hint))"
  fi
  status="$(http POST "$BASE_URL/login" \
    --data-urlencode "email=$ADMIN_EMAIL" \
    --data-urlencode "password=$QUICKSTART_PASSWORD" \
    --data-urlencode "csrf_token=$csrf")"
  if [[ "$status" != "302" ]]; then
    local login_err
    login_err="$(extract_error_text)"
    fail_with_logs "login rejected: ${login_err:-<no error text found>} (HTTP $status)"
  fi
  say_pass "login succeeds with the printed credentials"

  # A fresh CSRF token for the JSON POSTs below — login rotates the session,
  # which drops the anonymous token the register/login forms used. The
  # post-login page carries it in a <meta> tag, not the login/register forms'
  # hidden input, so this uses a different extractor.
  http GET "$BASE_URL/" >/dev/null
  CSRF_TOKEN="$(csrf_from_meta)"
  if [[ -z "$CSRF_TOKEN" ]]; then
    fail_with_logs "no CSRF token found on GET / after login ($(page_hint))"
  fi
}

smoke_chart() {
  local status
  status="$(http GET "$BASE_URL/api/chart/BTC?timeframe=1D")"
  if [[ "$status" != "200" ]]; then
    fail_with_logs "chart fetch failed (HTTP $status): $(cat "$TMP_BODY" | head -c 300)"
  fi
  if ! grep -qE '"ohlcv":[[:space:]]*\[[[:space:]]*\{' "$TMP_BODY"; then
    fail_with_logs "chart response has no non-empty ohlcv list: $(cat "$TMP_BODY" | head -c 300)"
  fi
  say_pass "a chart renders for BTC with no API key configured"
}

smoke_drawing() {
  local status drawing_id count
  status="$(http POST "$BASE_URL/api/drawings" \
    -H 'Content-Type: application/json' \
    -d '{"asset":"BTC","timeframe":"1D","drawing_type":"horizontal_line","price_level":50000,"label":"quickstart"}')"
  if [[ "$status" != "201" ]]; then
    fail_with_logs "drawing create failed (HTTP $status): $(cat "$TMP_BODY" | head -c 300)"
  fi
  drawing_id="$(json_field drawing_id)"
  if [[ -z "$drawing_id" ]]; then
    fail_with_logs "drawing create returned no drawing_id: $(cat "$TMP_BODY" | head -c 300)"
  fi

  status="$(http GET "$BASE_URL/api/drawings?asset=BTC")"
  if [[ "$status" != "200" ]]; then
    fail_with_logs "drawing list failed (HTTP $status): $(cat "$TMP_BODY" | head -c 300)"
  fi
  count="$(json_field count)"
  if [[ -z "$count" || "$count" -lt 1 ]]; then
    fail_with_logs "drawing list came back empty after create: $(cat "$TMP_BODY" | head -c 300)"
  fi
  say_pass "a drawing saves and survives a reload"
}

smoke_play() {
  local status play_id
  status="$(http POST "$BASE_URL/api/plays" \
    -H 'Content-Type: application/json' \
    -H "X-CSRFToken: $CSRF_TOKEN" \
    -d '{"title":"Quickstart play","type":"directional","direction":"long","primary_asset":"BTC","timeframe":"swing"}')"
  if [[ "$status" != "201" ]]; then
    fail_with_logs "play create failed (HTTP $status): $(cat "$TMP_BODY" | head -c 300)"
  fi
  play_id="$(json_field play.id)"
  if [[ -z "$play_id" ]]; then
    fail_with_logs "play create returned no id: $(cat "$TMP_BODY" | head -c 300)"
  fi

  local start=$SECONDS
  while true; do
    status="$(http POST "$BASE_URL/api/plays/$play_id/evaluate" -H "X-CSRFToken: $CSRF_TOKEN")"
    if [[ "$status" == "200" ]]; then
      break
    fi
    if [[ "$status" != "503" ]] || (( SECONDS - start >= READY_TIMEOUT )); then
      fail_with_logs "play evaluate failed (HTTP $status): $(cat "$TMP_BODY" | head -c 300)"
    fi
    note_warming
    printf '.'
    sleep 5
  done
  say_pass "a play can be created and evaluated"
}

smoke_mcp() {
  local status api_key tool_count
  status="$(http POST "$BASE_URL/api/keys" \
    -H 'Content-Type: application/json' \
    -H "X-CSRFToken: $CSRF_TOKEN" \
    -d '{"name":"quickstart"}')"
  if [[ "$status" != "201" ]]; then
    fail_with_logs "API key create failed (HTTP $status): $(cat "$TMP_BODY" | head -c 300)"
  fi
  api_key="$(json_field key)"
  if [[ -z "$api_key" ]]; then
    fail_with_logs "API key create returned no key: $(cat "$TMP_BODY" | head -c 300)"
  fi

  status="$(http POST "$BASE_URL/mcp" \
    -H "X-API-Key: $api_key" \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
  if [[ "$status" != "200" ]]; then
    fail_with_logs "MCP tools/list failed (HTTP $status): $(cat "$TMP_BODY" | head -c 300)"
  fi
  tool_count="$(json_field result.tools len)"
  if [[ -z "$tool_count" || "$tool_count" -lt 1 ]]; then
    fail_with_logs "MCP tools/list reported zero tools: $(cat "$TMP_BODY" | head -c 300)"
  fi
  say_pass "the MCP tool list responds and reports its tool count ($tool_count tools)"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if [[ $CHECK_ONLY -eq 0 ]]; then
  preflight
fi

if [[ ! -f "$ENV_FILE" && $CHECK_ONLY -eq 1 ]]; then
  preflight_fail \
    "no $ENV_FILE found" \
    "run ./scripts/quickstart.sh (without --check) first to start the stack"
fi

generate_env

if [[ $CHECK_ONLY -eq 0 ]]; then
  # data-init, migrate, carrac and worker all build the SAME image
  # (carrac-dev). Building via `up -d --build` builds all four in parallel,
  # and on a host without the buildx plugin (compose falls back to the
  # classic builder) that races tagging carrac-dev:latest — one build
  # deletes the tag another just created and compose aborts with
  # "AlreadyExists" before any container starts. Build the image once,
  # from a single service, before anything else touches it.
  if [[ $VERBOSE -eq 1 ]]; then
    echo "Building the dev image ($COMPOSE_FILE)..."
    compose build carrac
    echo "Starting the dev stack..."
    compose up -d
  else
    : > "$BUILD_LOG"
    echo "Building the dev image ($COMPOSE_FILE); full log: $BUILD_LOG"
    quiet_step "the image build failed" compose build carrac
    echo "Starting the dev stack..."
    quiet_step "the stack failed to start" compose up -d
  fi
fi

wait_for_health

echo "Registering and logging in..."
register_and_login

echo "Running the stranger smoke:"
wait_for_ready
smoke_chart
smoke_drawing
smoke_play
smoke_mcp

echo
echo "Carrac is up: $BASE_URL"
echo "  email:    $ADMIN_EMAIL"
echo "  password: $QUICKSTART_PASSWORD"
