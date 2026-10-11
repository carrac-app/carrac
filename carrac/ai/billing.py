"""
AI credit billing — reserve/settle pattern with atomic deductions.

Flow:
1. check_and_reserve() — deduct conservative estimate BEFORE LLM call
2. LLM streams response, counts actual tokens
3. settle() — calculate actual cost, refund difference, log usage

Pricing is provider-aware:
- Anthropic: cache-aware (base input, cache write 1.25x, cache read 0.1x, output)
- OpenAI-compatible: flat per-token rates from config

Every settle() also increments a shared Redis daily-spend counter
(`mcd:ai:cost:today:<YYYY-MM-DD>`). Routes call check_global_budget() before
reserving credits — if the global raw API cost exceeds AI_GLOBAL_DAILY_USD_CAP
(default $30), new AI streams return 503 until the next UTC midnight.
"""

import logging
import os
from datetime import datetime, timezone

from carrac.ai.pricing import ANTHROPIC_RATES as _ANTHROPIC_RATES, ANTHROPIC_DEFAULT

logger = logging.getLogger('carrac.ai.billing')

_GLOBAL_SPEND_KEY_PREFIX = 'mcd:ai:cost:today:'
_GLOBAL_SPEND_TTL = 2 * 24 * 3600  # keep "yesterday" visible for 1 extra day

# Module-level Redis client cache (was current_app.extensions in Flask).
_billing_redis_client = None
_billing_redis_url: str | None = None


def _redis_client():
    """Return a redis client for the global spend counter, or None if unavailable."""
    global _billing_redis_client, _billing_redis_url
    try:
        url = os.getenv('REDIS_URL', '')
        if not url:
            return None
        if _billing_redis_client is not None and _billing_redis_url == url:
            return _billing_redis_client
        import redis
        client = redis.from_url(url, socket_connect_timeout=3, decode_responses=True)
        client.ping()
        _billing_redis_client = client
        _billing_redis_url = url
        return client
    except Exception as e:
        logger.debug("Global-budget redis unavailable: %s", e)
        return None


def _today_key() -> str:
    return f"{_GLOBAL_SPEND_KEY_PREFIX}{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


def _daily_cap_cents() -> int:
    """Read AI_GLOBAL_DAILY_USD_CAP (USD) from env, default $30, return cents."""
    raw = os.getenv('AI_GLOBAL_DAILY_USD_CAP', '30')
    try:
        return max(1, int(round(float(raw) * 100)))
    except (TypeError, ValueError):
        return 30 * 100


def get_global_ai_spend_cents_today() -> int:
    """Return today's shared raw API spend across all users, in cents."""
    client = _redis_client()
    if client is None:
        return 0
    try:
        val = client.get(_today_key())
        return int(val) if val else 0
    except Exception as e:
        logger.warning("Global-budget read failed: %s", e)
        return 0


def _increment_global_spend(cents: int) -> None:
    """INCRBY today's raw-API-spend counter; refresh TTL. No-op if Redis is down."""
    if cents <= 0:
        return
    client = _redis_client()
    if client is None:
        return
    try:
        pipe = client.pipeline()
        pipe.incrby(_today_key(), int(cents))
        pipe.expire(_today_key(), _GLOBAL_SPEND_TTL)
        pipe.execute()
    except Exception as e:
        logger.error("Global AI budget: increment of %d cents failed: %s", cents, e)


def check_global_budget():
    """Return (ok, info_dict). If ok is False, caller should 503 the request.

    info_dict always carries current spend + cap (cents) so the caller can
    emit a precise friendly message.
    """
    cap = _daily_cap_cents()
    spent = get_global_ai_spend_cents_today()
    info = {'spent_cents': spent, 'cap_cents': cap, 'date': datetime.now(timezone.utc).strftime('%Y-%m-%d')}
    if spent >= cap:
        return False, info
    return True, info


def sanitize_for_llm(text, max_length=1000, source="ai_billing"):
    """Sanitise user input before sending to LLM.

    Thin wrapper that delegates to security.sanitize_for_llm so there is one
    hardened implementation. Applies NFKC normalisation, injection-pattern
    scrubbing, control-char stripping, and truncation.
    """
    from security import sanitize_for_llm as _strong
    if text is None or not isinstance(text, (str, bytes)):
        return ''
    return _strong(text, max_length=max_length, source=source).strip()


def calculate_cost_cents(input_tokens, output_tokens, usage=None):
    """Calculate actual cost in cents from token counts.

    For Anthropic: uses cache-aware pricing from the usage dict.
    For OpenAI-compatible: uses flat per-token rates from config.
    """
    provider = os.getenv('AI_PROVIDER', '').lower()
    model = os.getenv('AI_MODEL', '')

    if not provider:
        model_lower = model.lower() if model else ''
        if model_lower.startswith('claude-'):
            provider = 'anthropic'

    if provider == 'anthropic':
        return _anthropic_cost_cents(input_tokens, output_tokens, usage, model)

    # OpenAI-compatible: flat rates
    input_rate = float(os.getenv('AI_INPUT_RATE', '16.0'))
    output_rate = float(os.getenv('AI_OUTPUT_RATE', '80.0'))
    cost_usd = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    return max(1, round(cost_usd * 100))


def _anthropic_cost_cents(input_tokens, output_tokens, usage, model):
    """Precise Anthropic cost with cache-aware pricing.

    Splits input tokens into: standard input, cache write, cache read.
    Each has a different rate per MTok.
    """
    # Look up model-specific rates, fall back to Opus 4.6
    rates = _ANTHROPIC_RATES.get(model, _ANTHROPIC_RATES.get(ANTHROPIC_DEFAULT))

    cache_create = 0
    cache_read = 0
    if usage:
        cache_create = usage.get('cache_creation_input_tokens', 0) or 0
        cache_read = usage.get('cache_read_input_tokens', 0) or 0

    # Standard input = total input minus cached portions
    standard_input = max(0, input_tokens - cache_create - cache_read)

    cost_usd = (
        standard_input * rates['input'] +
        cache_create * rates['cache_write'] +
        cache_read * rates['cache_read'] +
        output_tokens * rates['output']
    ) / 1_000_000

    cents = round(cost_usd * 100, 2)

    if cache_create or cache_read:
        logger.info(
            f"Anthropic billing: {standard_input} standard + {cache_create} cache_write "
            f"+ {cache_read} cache_read input, {output_tokens} output = ${cost_usd:.4f}"
        )

    return max(1, round(cents))


def check_and_reserve(user_id, estimate_cents=None):
    """Atomically deduct conservative estimate before LLM call.

    Returns True if credits were reserved, False if insufficient balance.
    """
    from user_db import user_db
    if estimate_cents is None:
        estimate_cents = int(os.getenv('AI_ESTIMATE_CENTS', '25'))
    return user_db.deduct_ai_credits(user_id, estimate_cents)


def get_credit_markup():
    """Credit markup multiplier (env-tunable, defaults to 3x)."""
    return float(os.getenv('CREDIT_MARKUP', '3.0'))


def settle(user_id, estimate_cents, input_tokens, output_tokens,
           feature='chat', asset=None, model=None, usage=None,
           prompt_text=None, response_text=None, conversation_id=None,
           disconnected: bool = False, is_internal: bool = False):
    """After LLM call: refund/charge difference and log usage.

    Args:
        usage: Full usage dict from LLM client (includes cache token counts).
        prompt_text / response_text: pass the rendered strings so
            settle() can hash them + persist raw bodies under data/ai-logs/
            for 30-day admin-only retention. Pass `None` to opt out.
        conversation_id: stable id that links multiple turns.
        disconnected: True when the SSE client closed mid-stream.
            We charge the user for the real tokens already consumed by the
            LLM (Anthropic billed us regardless of whether the user saw the
            response), skip body persistence (the response_text is partial /
            untrusted), and ALWAYS increment the global spend counter so the
            daily AI cap can trip on disconnect-loops too. Without this, a
            tab-close loop would refund the full estimate while the LLM
            kept billing — uncapped cost-leak on Anthropic's side.
        is_internal: True skips ONLY the ai_usage_log
            row (the weekly usage review's spend-attribution query). Credit
            deduction/refund and the global daily-spend counter are
            unconditional — the company-wide safety cap must still see
            100% of real API spend regardless of attribution.

    Returns charged cents (what user pays, with markup applied).
    """
    from user_db import user_db

    raw_cents = calculate_cost_cents(input_tokens, output_tokens, usage=usage)
    markup = get_credit_markup()
    charged_cents = max(1, round(raw_cents * markup))

    # track shared raw API cost so check_global_budget()
    # can trip the daily cap. raw_cents is before markup — this is the real
    # money leaving our pocket, which is what the cap protects. ALWAYS
    # incremented, including on disconnect, because the LLM consumed those
    # tokens regardless of whether the user stayed connected.
    _increment_global_spend(raw_cents)

    diff = estimate_cents - charged_cents

    if diff > 0:
        user_db.refund_ai_credits(user_id, diff)
    elif diff < 0:
        # Try to charge extra; eat the loss if balance too low
        if not user_db.deduct_ai_credits(user_id, -diff):
            logger.warning(f"Ate {-diff}c loss for user {user_id}")

    # Hash the prompt + response so support can match a user
    # complaint to a row in ai_usage_log, and persist the raw bodies
    # separately with 30-day retention.
    #
    # On disconnect we still log the row (so usage analytics + the
    # daily-cap trip are accurate), but we DO NOT persist the partial
    # response body — it was never delivered to the user, so storing it as
    # an "AI response" is misleading. We still hash the prompt for trace.
    prompt_sha = _sha256_or_none(prompt_text)
    response_sha = None if disconnected else _sha256_or_none(response_text)
    try:
        if not disconnected and (prompt_text or response_text):
            _persist_ai_log(
                user_id=user_id, feature=feature, asset=asset, model=model or '',
                input_tokens=input_tokens, output_tokens=output_tokens,
                prompt_text=prompt_text, response_text=response_text,
                prompt_sha=prompt_sha, response_sha=response_sha,
                conversation_id=conversation_id,
            )
    except Exception as e:
        logger.error("AI log persistence failed for user %s (feature %s): %s", user_id, feature, e)

    # Log usage — cost_cents is raw API cost, charged_cents includes markup.
    # Tag disconnect-flagged rows with a feature suffix so dashboards can
    # split disconnect-cost vs delivered-cost without a schema change.
    log_feature = f"{feature}:disconnect" if disconnected else feature
    if not is_internal:
        try:
            user_db.log_ai_usage(
                user_id=user_id, feature=log_feature, asset=asset,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cost_cents=raw_cents, charged_cents=charged_cents,
                model=model or '',
                prompt_sha256=prompt_sha, response_sha256=response_sha,
                conversation_id=conversation_id,
            )
        except Exception as e:
            # On disconnect we don't want a log failure to mask the credit charge.
            logger.warning(f"ai_usage_log insert failed for user {user_id}: {e}")

    # Tier is subscription-based — don't downgrade on zero balance.
    # Users keep their tier until subscription expires.

    return charged_cents


def _sha256_or_none(text):
    if text is None:
        return None
    import hashlib
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()


def _persist_ai_log(*, user_id, feature, asset, model, input_tokens, output_tokens,
                    prompt_text, response_text, prompt_sha, response_sha,
                    conversation_id):
    """Append one JSONL line per AI call to data/ai-logs/<date>/
    <user_id>/<conversation_id>.jsonl for admin review. Bodies are retained
    30 days by scripts/ai_log_retention_prune.py.
    """
    import json as _json
    import os as _os
    from datetime import datetime as _dt, timezone as _tz

    root = _os.environ.get('AI_LOG_DIR') or _os.path.join(_os.environ.get('DATA_DIR', 'data'), 'ai-logs')
    today = _dt.now(_tz.utc).strftime('%Y-%m-%d')
    conv = conversation_id or 'adhoc'
    safe_conv = ''.join(c for c in str(conv) if c.isalnum() or c in '-_')[:64] or 'adhoc'
    dir_ = _os.path.join(root, today, str(int(user_id)))
    _os.makedirs(dir_, exist_ok=True)
    path = _os.path.join(dir_, f"{safe_conv}.jsonl")
    entry = {
        'ts': _dt.now(_tz.utc).replace(tzinfo=None).isoformat() + 'Z',
        'feature': feature,
        'asset': asset,
        'model': model,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'prompt_sha256': prompt_sha,
        'response_sha256': response_sha,
        'prompt': prompt_text,
        'response': response_text,
    }
    with open(path, 'a', encoding='utf-8') as f:
        f.write(_json.dumps(entry, ensure_ascii=False) + '\n')
