#!/usr/bin/env bash
# Optional-keys helper for .env.
#
# Walks through the optional features of .env.example (public address,
# internal key, market data, AI, email, notifications, Telegram, error
# tracking, sign-up) and writes the values you give into .env. It only
# touches the lines you answer, never writes an empty value (an empty KEY=
# reaches the app as "" which is not the same as unset), and never prints a
# secret back. Re-run it any time.
#
# Usage:
#   ./scripts/configure.sh                   interactive walk-through
#   ./scripts/configure.sh --list            which optional keys are set
#   ./scripts/configure.sh --set K=V [K=V]   set keys without questions
#   ./scripts/configure.sh --env-file PATH   another file than ./.env
#
# Needs: bash, grep. openssl only when you let it generate a key.
set -euo pipefail

ENV_FILE=".env"
MODE="interactive"
SETS=()
while (($#)); do
    case "$1" in
        --list) MODE="list" ;;
        --set) MODE="set" ;;
        --env-file) ENV_FILE="$2"; shift ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *=*) SETS+=("$1") ;;
        *) echo "configure.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE="$(dirname "$ENV_FILE")/.env.example"
[[ -f "$EXAMPLE" ]] || EXAMPLE="$SCRIPT_DIR/../.env.example"

# ── Known keys: KEY|secret?|group|prompt ─────────────────────────────────────
# Order matters: it is the order the walk-through asks in.
KEYS=(
    'SITE_URL|no|address|Public address of this instance, e.g. https://carrac.example.com'
    'SUPPORT_EMAIL|no|address|Support address shown on the legal and contact pages'
    'DISCORD_INVITE_URL|no|address|Community invite link (empty hides the Discord links)'
    'SCREENSHOT_API_KEY|yes|internal|Internal key for server-side screenshots, chart refresh and the report engine'
    'TELEGRAM_TOKEN_ENCRYPTION_KEY|yes|internal|Key that encrypts stored secrets (Telegram tokens, exchange API secrets, webhook signing secrets) at rest'
    'FRED_API_KEY|yes|market|FRED key for the macro panels (free at fred.stlouisfed.org)'
    'FINNHUB_API_KEY|yes|market|Finnhub key for live US stock ticks (free at finnhub.io)'
    'AI_PROVIDER|no|ai|AI provider: anthropic or openai'
    'ANTHROPIC_API_KEY|yes|ai|Anthropic API key'
    'AI_API_KEY|yes|ai|API key for the OpenAI-compatible endpoint'
    'AI_BASE_URL|no|ai|Chat-completions URL of the OpenAI-compatible endpoint'
    'AI_MODEL|no|ai|Model name'
    'BREVO_SMTP_HOST|no|email|SMTP host'
    'BREVO_SMTP_PORT|no|email|SMTP port (587 for STARTTLS)'
    'BREVO_SMTP_USER|no|email|SMTP user'
    'BREVO_SMTP_PASS|yes|email|SMTP password'
    'BREVO_FROM_EMAIL|no|email|From address for verification and reset emails'
    'BREVO_FROM_NAME|no|email|From name'
    'NTFY_BASE|no|notify|ntfy server for operator alerts, e.g. https://ntfy.example.com'
    'NTFY_CARRAC_TOPIC|yes|notify|ntfy topic for operator alerts'
    'NTFY_MARKET_TOPIC|yes|notify|ntfy topic for market alerts'
    'NTFY_BASE_HOST|no|notify|Your own ntfy host users may send their alerts to'
    'VAPID_PUBLIC_KEY|no|notify|Web-push VAPID public key (npx web-push generate-vapid-keys)'
    'VAPID_PRIVATE_KEY|yes|notify|Web-push VAPID private key'
    'VAPID_SUBJECT|no|notify|Web-push contact, e.g. mailto:you@example.com'
    'TELEGRAM_BOT_TOKEN|yes|telegram|Telegram bot token for operator alerts and report delivery'
    'TELEGRAM_CHAT_ID|no|telegram|Telegram chat id to deliver to'
    'SENTRY_DSN|yes|errors|Server-side Sentry-compatible DSN'
    'SENTRY_BROWSER_DSN|no|errors|Browser-side public DSN (optional)'
    'TURNSTILE_SITE_KEY|no|signup|Cloudflare Turnstile site key'
    'TURNSTILE_SECRET_KEY|yes|signup|Cloudflare Turnstile secret key'
    'SIGNUPS_ENABLED|no|signup|Public sign-up open? true or false'
)
GROUPS_IN_ORDER=(address internal market ai email notify telegram errors signup)
group_title() {
    case "$1" in
        address) echo "Public address and contact";;
        internal) echo "Internal key and at-rest encryption (recommended)";;
        market) echo "Market data keys (FRED macro, Finnhub US stocks)";;
        ai) echo "AI features (journal review, natural-language queries)";;
        email) echo "Outbound email (verification and password reset)";;
        notify) echo "Operator and web-push notifications";;
        telegram) echo "Telegram mirror";;
        errors) echo "Error tracking (Sentry-compatible)";;
        signup) echo "Sign-up (bot check, open or invite-only)";;
    esac
}

# ── File helpers ─────────────────────────────────────────────────────────────
ensure_env() {
    [[ -f "$ENV_FILE" ]] && return 0
    [[ -f "$EXAMPLE" ]] || { echo "configure.sh: neither $ENV_FILE nor $EXAMPLE exists" >&2; exit 1; }
    cp "$EXAMPLE" "$ENV_FILE"
    echo "Created $ENV_FILE from $(basename "$EXAMPLE"). The four required values at its top are"
    echo "still placeholders: run ./scripts/quickstart.sh, or set them by hand."
}

current_value() {   # prints the value of an uncommented KEY=, or nothing
    grep -m1 -E "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true
}

is_set() { grep -qE "^$1=.+" "$ENV_FILE" 2>/dev/null; }

upsert() {          # upsert KEY VALUE — rewrites one line, keeps every other byte
    local key="$1" value="$2" tmp line done_=0
    tmp="$(mktemp)"
    if grep -qE "^$key=" "$ENV_FILE"; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            if (( ! done_ )) && [[ "$line" == "$key="* ]]; then
                printf '%s=%s\n' "$key" "$value"; done_=1
            else
                printf '%s\n' "$line"
            fi
        done < "$ENV_FILE" > "$tmp"
    elif grep -qE "^# ?$key=" "$ENV_FILE"; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            if (( ! done_ )) && [[ "$line" =~ ^#\ ?$key= ]]; then
                printf '%s=%s\n' "$key" "$value"; done_=1
            else
                printf '%s\n' "$line"
            fi
        done < "$ENV_FILE" > "$tmp"
    else
        cat "$ENV_FILE" > "$tmp"
        [[ -s "$tmp" && "$(tail -c1 "$tmp" | od -An -c | tr -d ' ')" != '\n' ]] && printf '\n' >> "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
}

validate_pair() {   # KEY=VALUE → exits non-zero with a message on a bad pair
    local key="${1%%=*}" value="${1#*=}"
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "configure.sh: not a variable name: $key" >&2; return 1; }
    [[ -n "$value" ]] || { echo "configure.sh: refusing to write an empty value for $key (leave it unset instead)" >&2; return 1; }
    return 0
}

apply_hint() {
    echo
    echo "Applied to $ENV_FILE. Containers read it when they are created, so re-create them:"
    if grep -q '^QUICKSTART_PASSWORD=' "$ENV_FILE" 2>/dev/null; then
        echo "  docker compose -f docker-compose.dev.yml up -d --force-recreate"
    else
        echo "  docker compose up -d --force-recreate"
    fi
}

# ── Modes ────────────────────────────────────────────────────────────────────
mode_set() {
    (( ${#SETS[@]} )) || { echo "configure.sh: --set needs KEY=VALUE arguments" >&2; exit 2; }
    local p; for p in "${SETS[@]}"; do validate_pair "$p" || exit 1; done
    ensure_env
    for p in "${SETS[@]}"; do upsert "${p%%=*}" "${p#*=}"; echo "set ${p%%=*}"; done
    apply_hint
}

mode_list() {
    [[ -f "$ENV_FILE" ]] || { echo "configure.sh: $ENV_FILE does not exist (run ./scripts/quickstart.sh first)" >&2; exit 1; }
    local g entry key secret group prompt state
    for g in "${GROUPS_IN_ORDER[@]}"; do
        group_title "$g"
        for entry in "${KEYS[@]}"; do
            IFS='|' read -r key secret group prompt <<< "$entry"
            [[ "$group" == "$g" ]] || continue
            if is_set "$key"; then state="set"; else state="unset"; fi
            printf '  %-30s %s\n' "$key" "$state"
        done
    done
}

ask() {             # ask KEY secret prompt → sets REPLY_VALUE ("" = leave alone)
    local key="$1" secret="$2" prompt="$3" cur hint=""
    if is_set "$key"; then
        if [[ "$secret" == yes ]]; then hint=" [set, Enter keeps it]"; else cur="$(current_value "$key")"; hint=" [$cur, Enter keeps it]"; fi
    else
        hint=" [unset, Enter skips]"
    fi
    printf '  %s\n  %s%s: ' "$prompt" "$key" "$hint"
    if [[ "$secret" == yes ]]; then read -rs REPLY_VALUE || REPLY_VALUE=""; echo; else read -r REPLY_VALUE || REPLY_VALUE=""; fi
}

yes_no() {          # yes_no "question" → 0 for y/Y
    local a; printf '%s [y/N] ' "$1"; read -r a || a=""; echo
    [[ "$a" =~ ^[Yy] ]]
}

offer_generated() { # offer_generated KEY command-description command → sets REPLY_VALUE
    local key="$1" what="$2"; shift 2
    if ! is_set "$key" && command -v openssl >/dev/null 2>&1; then
        if yes_no "    Generate $key with $what?"; then REPLY_VALUE="$("$@")"; return 0; fi
    fi
    return 1
}

mode_interactive() {
    ensure_env
    echo "Carrac optional configuration — writes into $ENV_FILE. Enter keeps or skips a value."
    echo
    local admin secret_key
    admin="$(current_value ADMIN_EMAIL)"; secret_key="$(current_value SECRET_KEY)"
    if [[ "$admin" == *@example.* || "$secret_key" == generate-me-* || -z "$admin" ]]; then
        if yes_no "The required values at the top of $ENV_FILE are still placeholders. Set them now?"; then
            if command -v openssl >/dev/null 2>&1; then
                upsert POSTGRES_PASSWORD "$(openssl rand -base64 24 | tr -d '/+=' )"
                upsert REDIS_PASSWORD "$(openssl rand -base64 24 | tr -d '/+=')"
                upsert SECRET_KEY "$(openssl rand -base64 48 | tr -d '/+=')"
                echo "  generated POSTGRES_PASSWORD, REDIS_PASSWORD, SECRET_KEY"
            else
                echo "  openssl not found: set POSTGRES_PASSWORD, REDIS_PASSWORD and SECRET_KEY by hand"
            fi
            while :; do
                printf '  ADMIN_EMAIL (the address you will sign in with): '
                read -r admin || admin=""
                [[ -z "$admin" ]] && { echo "  left as is"; break; }
                [[ "$admin" == *@*.* && "$admin" != *@example.* ]] && { upsert ADMIN_EMAIL "$admin"; break; }
                echo "  that is not a usable address"
            done
            echo
        fi
    fi
    local g entry key secret group prompt changed=0
    for g in "${GROUPS_IN_ORDER[@]}"; do
        yes_no "$(group_title "$g")?" || continue
        for entry in "${KEYS[@]}"; do
            IFS='|' read -r key secret group prompt <<< "$entry"
            [[ "$group" == "$g" ]] || continue
            REPLY_VALUE=""
            case "$key" in
                SCREENSHOT_API_KEY) offer_generated "$key" "openssl rand -hex 32" openssl rand -hex 32 || ask "$key" "$secret" "$prompt" ;;
                TELEGRAM_TOKEN_ENCRYPTION_KEY) offer_generated "$key" "a random Fernet key" bash -c "openssl rand -base64 32 | tr '+/' '-_'" || ask "$key" "$secret" "$prompt" ;;
                *) ask "$key" "$secret" "$prompt" ;;
            esac
            [[ -n "$REPLY_VALUE" ]] && { upsert "$key" "$REPLY_VALUE"; changed=1; }
        done
        echo
    done
    if (( changed )); then apply_hint; else echo "Nothing changed."; fi
}

case "$MODE" in
    set) mode_set ;;
    list) mode_list ;;
    interactive) mode_interactive ;;
esac
