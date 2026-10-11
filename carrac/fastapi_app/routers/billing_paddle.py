"""Paddle webhook — FastAPI port.

Reuses all runtime-agnostic helpers from ``carrac.billing.routes`` directly:
  - ``_verify_paddle_signature``   — pure function, no Flask state
  - ``_inbox_record``              — DB-only
  - ``_inbox_mark_processed``      — DB-only
  - ``_inbox_record_error``        — DB-only
  - ``MAX_INBOX_RETRIES``          — constant
  - ``_handle_*`` event handlers   — DB-only (use ``user_db`` directly)

Auth model: HMAC signature only — no session / API-key Depends().
CSRF: ``/api/billing/paddle-webhook`` is already in ``_SKIP_PREFIXES``
      in ``csrf_middleware.py`` (mirrors the Flask ``@csrf.exempt`` decoration).
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from carrac.fastapi_app.limiter import limiter
from carrac.billing.routes import (
    MAX_INBOX_RETRIES,
    _handle_customer_created,
    _handle_subscription_activated,
    _handle_subscription_canceled,
    _handle_subscription_past_due,
    _handle_subscription_paused,
    _handle_subscription_resumed,
    _handle_subscription_updated,
    _handle_transaction_completed,
    _handle_transaction_payment_failed,
    _handle_transaction_refunded,
    _inbox_mark_processed,
    _inbox_record,
    _inbox_record_error,
    _verify_paddle_signature,
)
from carrac.config import Config
from carrac.notifications import send_telegram_message

logger = logging.getLogger("carrac.fastapi.billing_paddle")

router = APIRouter()


@router.post("/api/billing/paddle-webhook")
# Generous cap: Paddle sends from a handful of IPs, so per-IP buckets are
# effectively shared — a tight limit would 429 legit retry bursts. Idempotency
# markers make replays safe; this only blunts garbage floods.
@limiter.limit("600/minute")
async def paddle_webhook(request: Request) -> JSONResponse:
    """Paddle Billing v2 webhook — FastAPI port.

    Verified via Paddle-Signature header (HMAC-SHA256).
    CSRF exempt: ``/api/billing/paddle-webhook`` is in
    ``_SKIP_PREFIXES`` in ``csrf_middleware.py``.

    Critical: raw body is read via ``await request.body()`` BEFORE
    any JSON parsing so the HMAC is computed against the unmodified
    bytes (Paddle signs the raw wire body).
    """
    # ── 1. Check webhook secret is configured ─────────────────────────
    secret = Config.PADDLE_WEBHOOK_SECRET
    if not secret:
        # Severity mirrors the rule already in admin/health_checks.py::check_billing —
        # BOTH keys absent means payments are deliberately off (beta), which is
        # expected state, not an alarm; exactly ONE key set is a real
        # misconfiguration and has to stay loud.
        #
        # Logging this at ERROR unconditionally would be wrong: sentry's
        # LoggingIntegration files ERROR records as events, and
        # scripts/smoke-test.sh POSTs here on every run and ASSERTS this exact
        # 503, so every smoke run would raise a self-inflicted issue. The 503
        # stays; only the severity depends on the key state.
        if Config.PADDLE_API_KEY:
            logger.error(
                "PADDLE_WEBHOOK_SECRET missing while PADDLE_API_KEY is set — "
                "webhook cannot be verified"
            )
        else:
            logger.info(
                "Paddle webhook called while payments are disabled "
                "(no Paddle keys — beta); returning 503"
            )
        return JSONResponse({"error": "webhook secret not configured"}, status_code=503)

    # ── 2. Read RAW body BEFORE any parsing ───────────────────────────
    raw_bytes: bytes = await request.body()
    raw_body: str = raw_bytes.decode("utf-8", errors="replace")

    # ── 3. Verify HMAC signature ──────────────────────────────────────
    sig_header: str = request.headers.get("Paddle-Signature", "")

    if not _verify_paddle_signature(sig_header, raw_body, secret):
        logger.warning("Paddle webhook: signature verification failed")
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    # ── 4. Parse JSON ─────────────────────────────────────────────────
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Paddle webhook: invalid JSON")
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    event_type: str = payload.get("event_type", "")
    event_id: str = payload.get("event_id", "")
    data: dict = payload.get("data", {})

    # ── 5. Reject events without event_id (no idempotency guarantee) ──
    if not event_id:
        logger.warning(f"Paddle webhook: missing event_id for {event_type}; rejecting")
        return JSONResponse({"error": "missing event_id"}, status_code=400)

    logger.info(f"Paddle webhook: {event_type} (event_id={event_id})")

    # ── 6. Write inbox row BEFORE processing (idempotent via UNIQUE) ──
    # Offloaded but still awaited (sequential) so the inbox-write-before-dispatch
    # ordering guarantee is unchanged — see module docstring.
    await asyncio.to_thread(_inbox_record, event_id, event_type, payload)

    # ── 7. Dispatch to event handlers ─────────────────────────────────
    def _dispatch():
        if event_type == "transaction.completed":
            _handle_transaction_completed(data, event_id)
        elif event_type == "subscription.activated":
            _handle_subscription_activated(data, event_id)
        elif event_type == "subscription.updated":
            _handle_subscription_updated(data, event_id)
        elif event_type == "subscription.canceled":
            _handle_subscription_canceled(data, event_id)
        elif event_type == "transaction.refunded":
            _handle_transaction_refunded(data, event_id)
        elif event_type == "transaction.payment_failed":
            _handle_transaction_payment_failed(data, event_id)
        elif event_type == "subscription.past_due":
            _handle_subscription_past_due(data, event_id)
        elif event_type == "subscription.paused":
            _handle_subscription_paused(data, event_id)
        elif event_type == "subscription.resumed":
            _handle_subscription_resumed(data, event_id)
        elif event_type == "customer.created":
            _handle_customer_created(data, event_id)
        else:
            logger.info(f"Paddle webhook: unhandled event type {event_type}")

    processing_error: str | None = None
    try:
        await asyncio.to_thread(_dispatch)
    except Exception as exc:
        processing_error = str(exc)
        logger.error(
            f"Paddle webhook handler error ({event_type}): {exc}", exc_info=True
        )
        # Revenue-critical: surface on strike 1 rather than waiting for the
        # DLQ threshold (MAX_INBOX_RETRIES). sentry_sdk no-ops when the SDK
        # is uninitialised (no SENTRY_DSN); the try/except is defense-in-depth
        # against any SDK-internal error (mirrors crm_reconcile.py / waitlist_release.py).
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass

    # ── 8. Update inbox row: processed or error ───────────────────────
    # Sequential awaits preserve the mark-processed/record-error ordering
    # relative to dispatch above (no concurrency introduced).
    if processing_error is None:
        await asyncio.to_thread(_inbox_mark_processed, event_id)
    else:
        retry_count = await asyncio.to_thread(_inbox_record_error, event_id, processing_error)
        if retry_count >= MAX_INBOX_RETRIES:
            try:
                await asyncio.to_thread(
                    send_telegram_message,
                    f"\U0001f6a8 *Paddle DLQ*\nEvent `{event_id}` ({event_type}) failed "
                    f"{retry_count} times and is in the DLQ. Last error: {processing_error[:200]}"
                )
            except Exception:
                pass

    return JSONResponse({"status": "ok"}, status_code=200)
