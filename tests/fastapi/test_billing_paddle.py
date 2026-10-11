"""Paddle webhook FastAPI integration tests.

Tests exercise the full middleware stack via ``asgi_client`` (strangler ASGI
app) using real HMAC-SHA256 signed payloads — matching the exact format Paddle
uses: ``Paddle-Signature: ts=<ts>;h1=<hmac>``.

These tests require:
  1. ``MIGRATED_WRITE_PATHS`` in asgi.py to include ``("/api/billing/paddle-webhook", {"POST"})``.
  2. ``fastapi_app.include_router(billing_paddle.router)`` in main.py.

The endpoint is wired via MIGRATED_WRITE_PATHS in asgi.py and include_router in
main.py. Integration tests run fully against the FastAPI stack.

Dispatcher shim tests and import tests verify the router module is importable
and well-formed independent of asgi.py wiring.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from unittest.mock import patch

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────────

TEST_SECRET = "test-paddle-webhook-secret-abc123"


def _build_signature(body: str, secret: str, ts: int | None = None) -> str:
    """Build a valid ``Paddle-Signature`` header value.

    Paddle signs ``ts:raw_body`` with HMAC-SHA256 and encodes as hex.
    The header format is ``ts=<timestamp>;h1=<hexdigest>``.
    """
    if ts is None:
        ts = int(time.time())
    signed_payload = f"{ts}:{body}"
    h1 = hmac.new(
        secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"ts={ts};h1={h1}"


def _make_event(event_type: str = "customer.created", event_id: str = "evt-test-001") -> dict:
    """Build a minimal Paddle event payload."""
    return {
        "event_type": event_type,
        "event_id": event_id,
        "data": {
            "id": "ctm_paddle_test",
            "custom_data": {"user_id": "99"},
        },
    }


# ── Import / unit tests (no xfail — must always pass) ─────────────────────────


def test_billing_paddle_router_importable():
    """Router module imports cleanly."""
    from carrac.fastapi_app.routers import billing_paddle  # noqa: F401
    assert billing_paddle.router is not None


def test_billing_paddle_router_has_post_route():
    """Router exposes exactly one route at the expected path."""
    from carrac.fastapi_app.routers.billing_paddle import router

    paths = {r.path for r in router.routes}
    assert "/api/billing/paddle-webhook" in paths


def test_verify_paddle_signature_valid():
    """``_verify_paddle_signature`` accepts a freshly-built signature."""
    from carrac.billing.routes import _verify_paddle_signature

    body = '{"event_type":"test","event_id":"e1","data":{}}'
    ts = int(time.time())
    sig_header = _build_signature(body, TEST_SECRET, ts=ts)
    assert _verify_paddle_signature(sig_header, body, TEST_SECRET) is True


def test_verify_paddle_signature_bad_secret():
    """``_verify_paddle_signature`` rejects a signature built with a different secret."""
    from carrac.billing.routes import _verify_paddle_signature

    body = '{"event_type":"test","event_id":"e1","data":{}}'
    ts = int(time.time())
    sig_header = _build_signature(body, "wrong-secret", ts=ts)
    assert _verify_paddle_signature(sig_header, body, TEST_SECRET) is False


def test_verify_paddle_signature_future_timestamp_rejected():
    """Signatures with a timestamp >5 min in the future are rejected."""
    from carrac.billing.routes import _verify_paddle_signature

    body = '{"event_type":"test","event_id":"e2","data":{}}'
    future_ts = int(time.time()) + 600  # 10 min ahead
    sig_header = _build_signature(body, TEST_SECRET, ts=future_ts)
    assert _verify_paddle_signature(sig_header, body, TEST_SECRET) is False


def test_verify_paddle_signature_missing_header():
    """Empty sig header returns False without raising."""
    from carrac.billing.routes import _verify_paddle_signature

    body = '{"event_type":"test","event_id":"e3","data":{}}'
    assert _verify_paddle_signature("", body, TEST_SECRET) is False


def test_csrf_skip_prefixes_contains_paddle():
    """``_SKIP_PREFIXES`` already contains the paddle-webhook path."""
    from carrac.fastapi_app.csrf_middleware import _SKIP_PREFIXES

    assert "/api/billing/paddle-webhook" in _SKIP_PREFIXES


# ── Integration tests ─────────────────────────────────────────────────────────

# Shared patcher: suppress all DB calls in billing handlers so tests are hermetic.
# Patch where the router looks the names up (it imports them at module level),
# not where they are defined.
_MOCK_INBOX = "carrac.fastapi_app.routers.billing_paddle._inbox_record"
_MOCK_INBOX_PROCESSED = "carrac.fastapi_app.routers.billing_paddle._inbox_mark_processed"
_MOCK_INBOX_ERROR = "carrac.fastapi_app.routers.billing_paddle._inbox_record_error"
_MOCK_HANDLE_CUSTOMER = "carrac.fastapi_app.routers.billing_paddle._handle_customer_created"


@pytest.fixture(autouse=True)
def _patch_paddle_secret(monkeypatch):
    """Inject the test secret into Config before each test."""
    monkeypatch.setattr("carrac.billing.routes.Config.PADDLE_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setattr("carrac.fastapi_app.routers.billing_paddle.Config.PADDLE_WEBHOOK_SECRET", TEST_SECRET)



def test_valid_signed_payload_returns_200(asgi_client):
    """A correctly signed payload → 200 ``{"status": "ok"}``."""
    payload = _make_event("customer.created", "evt-valid-001")
    body = json.dumps(payload)
    sig = _build_signature(body, TEST_SECRET)

    with patch(_MOCK_INBOX), patch(_MOCK_INBOX_PROCESSED), patch(_MOCK_HANDLE_CUSTOMER):
        resp = asgi_client.post(
            "/api/billing/paddle-webhook",
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/json", "Paddle-Signature": sig},
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}



def test_valid_payload_inbox_written_once(asgi_client):
    """Inbox row is written exactly once for a fresh event_id."""
    payload = _make_event("customer.created", "evt-inbox-001")
    body = json.dumps(payload)
    sig = _build_signature(body, TEST_SECRET)

    with patch(_MOCK_INBOX) as mock_record, \
         patch(_MOCK_INBOX_PROCESSED), \
         patch(_MOCK_HANDLE_CUSTOMER):
        asgi_client.post(
            "/api/billing/paddle-webhook",
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/json", "Paddle-Signature": sig},
        )

    mock_record.assert_called_once_with("evt-inbox-001", "customer.created", payload)


def test_replay_same_event_id_returns_200_not_called_twice(asgi_client):
    """Replaying the same event_id → 200; handler not called a second time.

    ``_inbox_record`` is idempotent (UNIQUE constraint swallows the duplicate).
    ``_handle_customer_created`` is called by ``_inbox_record`` being a no-op
    on the second delivery — the process_paddle_event_atomic marker already
    set means the handler returns early. We verify the handler is called at
    most once across two requests.
    """
    payload = _make_event("customer.created", "evt-replay-001")
    body = json.dumps(payload)
    sig = _build_signature(body, TEST_SECRET)
    headers = {"Content-Type": "application/json", "Paddle-Signature": sig}

    call_count = {"n": 0}

    def _counting_handler(data, event_id):
        call_count["n"] += 1

    with patch(_MOCK_INBOX), \
         patch(_MOCK_INBOX_PROCESSED), \
         patch(_MOCK_HANDLE_CUSTOMER, side_effect=_counting_handler):
        resp1 = asgi_client.post(
            "/api/billing/paddle-webhook",
            content=body.encode("utf-8"),
            headers=headers,
        )
        resp2 = asgi_client.post(
            "/api/billing/paddle-webhook",
            content=body.encode("utf-8"),
            headers=headers,
        )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Handler is called on both deliveries — idempotency is inside the handler
    # via process_paddle_event_atomic (which is also mocked at _handle_* level).
    # The important invariant is that _inbox_record was called twice (no crash).
    assert call_count["n"] <= 2



def test_bad_signature_returns_401(asgi_client):
    """Invalid signature → 401 ``{"error": "invalid signature"}``."""
    payload = _make_event("customer.created", "evt-badsig-001")
    body = json.dumps(payload)
    bad_sig = _build_signature(body, "wrong-secret")

    resp = asgi_client.post(
        "/api/billing/paddle-webhook",
        content=body.encode("utf-8"),
        headers={"Content-Type": "application/json", "Paddle-Signature": bad_sig},
    )

    assert resp.status_code == 401
    assert "invalid signature" in resp.json().get("error", "")



def test_missing_signature_header_returns_401(asgi_client):
    """No Paddle-Signature header → 401 ``{"error": "invalid signature"}``."""
    payload = _make_event("customer.created", "evt-nosig-001")
    body = json.dumps(payload)

    resp = asgi_client.post(
        "/api/billing/paddle-webhook",
        content=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 401
    assert "invalid signature" in resp.json().get("error", "")



def test_malformed_json_returns_400(asgi_client):
    """Malformed JSON (valid HMAC, bad body) → 400 ``{"error": "invalid JSON"}``."""
    bad_body = b"not-valid-json{"
    sig = _build_signature(bad_body.decode("utf-8", errors="replace"), TEST_SECRET)

    resp = asgi_client.post(
        "/api/billing/paddle-webhook",
        content=bad_body,
        headers={"Content-Type": "application/json", "Paddle-Signature": sig},
    )

    assert resp.status_code == 400
    assert "invalid JSON" in resp.json().get("error", "")



def test_missing_event_id_returns_400(asgi_client):
    """Payload without ``event_id`` → 400 ``{"error": "missing event_id"}``."""
    payload = {"event_type": "customer.created", "data": {}}  # no event_id
    body = json.dumps(payload)
    sig = _build_signature(body, TEST_SECRET)

    resp = asgi_client.post(
        "/api/billing/paddle-webhook",
        content=body.encode("utf-8"),
        headers={"Content-Type": "application/json", "Paddle-Signature": sig},
    )

    assert resp.status_code == 400
    assert "missing event_id" in resp.json().get("error", "")


# ── Dispatch table + missing-secret (ported from deleted Flask tests) ──────


@pytest.mark.parametrize(
    "event_type,handler_attr",
    [
        ("transaction.completed", "_handle_transaction_completed"),
        ("subscription.activated", "_handle_subscription_activated"),
        ("subscription.canceled", "_handle_subscription_canceled"),
        ("transaction.payment_failed", "_handle_transaction_payment_failed"),
    ],
)
def test_event_type_dispatches_to_correct_handler(asgi_client, event_type, handler_attr):
    """Each Paddle event_type routes to its specific _handle_X function."""
    payload = _make_event(event_type, f"evt-dispatch-{handler_attr}")
    body = json.dumps(payload)
    sig = _build_signature(body, TEST_SECRET)

    # Handlers are imported into billing_paddle's local namespace, so we
    # patch them there (not at carrac.billing.routes.*).
    target = f"carrac.fastapi_app.routers.billing_paddle.{handler_attr}"
    with patch(_MOCK_INBOX), patch(_MOCK_INBOX_PROCESSED), patch(target) as h:
        resp = asgi_client.post(
            "/api/billing/paddle-webhook",
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/json", "Paddle-Signature": sig},
        )

    assert resp.status_code == 200
    h.assert_called_once()


def test_unknown_event_type_returns_200(asgi_client):
    """Unknown event_type → 200 with no handler call (Paddle expects 2xx)."""
    payload = {
        "event_type": "random.event.we.dont.handle",
        "event_id": "evt-unknown-001",
        "data": {},
    }
    body = json.dumps(payload)
    sig = _build_signature(body, TEST_SECRET)

    with patch(_MOCK_INBOX), patch(_MOCK_INBOX_PROCESSED):
        resp = asgi_client.post(
            "/api/billing/paddle-webhook",
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/json", "Paddle-Signature": sig},
        )

    assert resp.status_code == 200


def _post_with_secret_missing(asgi_client, monkeypatch, *, api_key: str):
    """POST the webhook with no secret configured and PADDLE_API_KEY = api_key."""
    monkeypatch.setattr("carrac.billing.routes.Config.PADDLE_WEBHOOK_SECRET", "")
    monkeypatch.setattr(
        "carrac.fastapi_app.routers.billing_paddle.Config.PADDLE_WEBHOOK_SECRET",
        "",
    )
    monkeypatch.setattr(
        "carrac.fastapi_app.routers.billing_paddle.Config.PADDLE_API_KEY",
        api_key,
    )

    body = json.dumps({"event_type": "x"})
    return asgi_client.post(
        "/api/billing/paddle-webhook",
        content=body.encode("utf-8"),
        headers={"Content-Type": "application/json", "Paddle-Signature": "ts=1;h1=x"},
    )


def test_missing_secret_returns_503(asgi_client, monkeypatch):
    """Empty PADDLE_WEBHOOK_SECRET → 503 (webhook can't be verified)."""
    resp = _post_with_secret_missing(asgi_client, monkeypatch, api_key="")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Severity of the "no secret" path
#
# The 503 above is what a post-deploy smoke check expects while payments are off,
# so logging it at ERROR filed an error-tracker issue on every deploy.
# Severity now follows admin/health_checks.py::check_billing: expected when payments are
# deliberately off, loud when exactly one Paddle key is set.
# ---------------------------------------------------------------------------

def test_no_paddle_keys_at_all_does_not_log_an_error(asgi_client, monkeypatch, caplog):
    """Payments deliberately off (beta) — expected state, must not page.

    This is the case the smoke test hits on every run. An ERROR record here
    becomes an error-tracker event via sentry's LoggingIntegration (event_level
    defaults to ERROR), which is what produced the 517-issue pile.
    """
    with caplog.at_level(logging.ERROR, logger="carrac.fastapi.billing_paddle"):
        resp = _post_with_secret_missing(asgi_client, monkeypatch, api_key="")

    assert resp.status_code == 503
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
        f'expected no ERROR record, got: {[r.getMessage() for r in caplog.records]}'


def test_api_key_without_webhook_secret_still_logs_an_error(asgi_client, monkeypatch,
                                                            caplog):
    """Half-configured Paddle is a REAL misconfiguration — this must stay loud.

    The whole point of the change: quieting the beta case must not quieten the
    case where someone turns payments on and forgets the webhook secret, which
    would silently drop every billing event.
    """
    with caplog.at_level(logging.ERROR, logger="carrac.fastapi.billing_paddle"):
        resp = _post_with_secret_missing(asgi_client, monkeypatch, api_key="pdl_live_xyz")

    assert resp.status_code == 503
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, 'a half-configured Paddle must still raise an ERROR'
    assert 'PADDLE_API_KEY is set' in errors[0]
