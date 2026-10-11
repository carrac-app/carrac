"""Journal AI endpoints tests.

Tests cover:

Dispatcher shim unit tests (no network, no app context):
- ExactPath/PrefixPath matching logic for the 4 journal AI routes.
- MIGRATED_WRITE_PATHS/PREFIXES entries.

Pydantic model unit tests:
- ``JournalAiQueryRequest``, ``JournalWeeklyReviewRequest``, ``JournalAiReviewBody``
  field constraints and ``extra="forbid"`` behaviour.

Integration tests via ``asgi_client`` (login is injected with ``login_as``):
- Auth gate → 302/redirect for all 4 endpoints.
- ``/api/journal/trades/{id}/ai-review``:
    - non-existent trade / not-owned → 404
    - not-closed trade → 400
    - happy path → 200 JSON
- ``/api/journal/sessions/{id}/ai-summary``:
    - not-owned session → 404
    - happy path → 200 JSON
- ``/api/journal/ai-review/weekly``:
    - ``days`` out of range → 400
    - happy path → 200 JSON
- ``/api/journal/ai-query`` SSE:
    - empty question → 400
    - valid question → SSE chunks flow (status 200, text/event-stream)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pydantic
import pytest

from carrac.asgi import (
    MIGRATED_WRITE_PATHS,
    MIGRATED_WRITE_PREFIXES,
    ExactPath,
    PrefixPath,
)


# ---------------------------------------------------------------------------
# MIGRATED_WRITE_PATHS / MIGRATED_WRITE_PREFIXES entry assertions
# ---------------------------------------------------------------------------


def test_journal_ai_query_in_migrated_write_paths():
    assert (
        "/api/journal/ai-query",
        frozenset({"POST"}),
    ) in MIGRATED_WRITE_PATHS


def test_journal_ai_review_weekly_in_migrated_write_paths():
    assert (
        "/api/journal/ai-review/weekly",
        frozenset({"POST"}),
    ) in MIGRATED_WRITE_PATHS


def test_journal_trades_prefix_includes_post():
    """``/api/journal/trades/`` already includes POST."""
    assert (
        "/api/journal/trades/",
        frozenset({"PATCH", "DELETE", "POST"}),
    ) in MIGRATED_WRITE_PREFIXES


def test_journal_sessions_prefix_includes_post():
    """``/api/journal/sessions/`` prefix must include POST for ai-summary."""
    assert (
        "/api/journal/sessions/",
        frozenset({"PATCH", "DELETE", "POST"}),
    ) in MIGRATED_WRITE_PREFIXES


# ---------------------------------------------------------------------------
# ExactPath/PrefixPath routing logic (self-contained, always pass)
# ---------------------------------------------------------------------------


def test_ai_query_exact_path_matches_post_only():
    ep = ExactPath(
        "/api/journal/ai-query", app=None, methods=frozenset({"POST"})
    )
    base = {"type": "http", "path": "/api/journal/ai-query"}
    assert ep.matches({**base, "method": "POST"})[0].value > 0
    assert ep.matches({**base, "method": "GET"})[0].value == 0


def test_ai_review_weekly_exact_path_matches_post_only():
    ep = ExactPath(
        "/api/journal/ai-review/weekly", app=None, methods=frozenset({"POST"})
    )
    base = {"type": "http", "path": "/api/journal/ai-review/weekly"}
    assert ep.matches({**base, "method": "POST"})[0].value > 0
    assert ep.matches({**base, "method": "GET"})[0].value == 0


def test_trades_prefix_post_matches_ai_review_subroute():
    """The existing trades/ prefix catches ``POST /<id>/ai-review`` (3-segment)."""
    pp = PrefixPath(
        "/api/journal/trades/",
        app=None,
        methods=frozenset({"PATCH", "DELETE", "POST"}),
    )
    scope = {
        "type": "http",
        "path": "/api/journal/trades/5/ai-review",
        "method": "POST",
    }
    match, _ = pp.matches(scope)
    assert match.value > 0, "POST /<id>/ai-review must match trades/ prefix"


def test_sessions_prefix_post_matches_ai_summary_subroute():
    """A sessions/ prefix WITH POST would catch ``POST /<id>/ai-summary``."""
    pp = PrefixPath(
        "/api/journal/sessions/",
        app=None,
        methods=frozenset({"PATCH", "DELETE", "POST"}),
    )
    scope = {
        "type": "http",
        "path": "/api/journal/sessions/3/ai-summary",
        "method": "POST",
    }
    match, _ = pp.matches(scope)
    assert match.value > 0, "POST /<id>/ai-summary must match sessions/ prefix when POST is included"


# ---------------------------------------------------------------------------
# Pydantic model unit tests (no routing required)
# ---------------------------------------------------------------------------


def test_journal_ai_query_valid():
    from carrac.fastapi_app.models.journal_ai import JournalAiQueryRequest

    req = JournalAiQueryRequest(question="Which asset has the best win rate?")
    assert req.question == "Which asset has the best win rate?"


def test_journal_ai_query_empty_question_rejected():
    from carrac.fastapi_app.models.journal_ai import JournalAiQueryRequest

    with pytest.raises(pydantic.ValidationError):
        JournalAiQueryRequest(question="")


def test_journal_ai_query_missing_question_rejected():
    from carrac.fastapi_app.models.journal_ai import JournalAiQueryRequest

    with pytest.raises(pydantic.ValidationError):
        JournalAiQueryRequest()


def test_journal_ai_query_question_too_long_rejected():
    from carrac.fastapi_app.models.journal_ai import JournalAiQueryRequest

    with pytest.raises(pydantic.ValidationError):
        JournalAiQueryRequest(question="x" * 501)


def test_journal_ai_query_extra_field_rejected():
    from carrac.fastapi_app.models.journal_ai import JournalAiQueryRequest

    with pytest.raises(pydantic.ValidationError) as exc_info:
        JournalAiQueryRequest(question="test", evil="injection")
    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" for e in errors)


def test_journal_weekly_review_default_days():
    from carrac.fastapi_app.models.journal_ai import JournalWeeklyReviewRequest

    req = JournalWeeklyReviewRequest()
    assert req.days == 7


def test_journal_weekly_review_custom_days_valid():
    from carrac.fastapi_app.models.journal_ai import JournalWeeklyReviewRequest

    req = JournalWeeklyReviewRequest(days=30)
    assert req.days == 30


def test_journal_weekly_review_days_too_low_rejected():
    from carrac.fastapi_app.models.journal_ai import JournalWeeklyReviewRequest

    with pytest.raises(pydantic.ValidationError):
        JournalWeeklyReviewRequest(days=3)


def test_journal_weekly_review_days_too_high_rejected():
    from carrac.fastapi_app.models.journal_ai import JournalWeeklyReviewRequest

    with pytest.raises(pydantic.ValidationError):
        JournalWeeklyReviewRequest(days=200)


def test_journal_ai_review_body_empty_valid():
    from carrac.fastapi_app.models.journal_ai import JournalAiReviewBody

    req = JournalAiReviewBody()
    assert req is not None


def test_journal_ai_review_body_extra_rejected():
    from carrac.fastapi_app.models.journal_ai import JournalAiReviewBody

    with pytest.raises(pydantic.ValidationError) as exc_info:
        JournalAiReviewBody(unexpected="bad")
    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" for e in errors)


# ---------------------------------------------------------------------------
# Auth fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def premium_user_dict():
    return {"id": 42, "email": "user@example.com", "tier": "premium", "is_admin": False}


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/journal/trades/1/ai-review", {}),
        ("/api/journal/sessions/1/ai-summary", {}),
        ("/api/journal/ai-review/weekly", {}),
        ("/api/journal/ai-query", {"question": "What is my best setup?"}),
    ],
)
def test_unauthenticated_redirects(asgi_client, path, body):
    """All journal AI endpoints → 302 redirect to /login for unauthenticated users."""
    resp = asgi_client.post(path, json=body, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("location", "")


def test_ai_review_trade_not_found(asgi_client, login_as, premium_user_dict):
    """Non-existent trade → 404."""
    with login_as(premium_user_dict):
        with patch("carrac.content_db.get_content_db") as mock_factory:
            mock_db = MagicMock()
            mock_db.get_journal_entry.return_value = None
            mock_factory.return_value = mock_db
            resp = asgi_client.post("/api/journal/trades/999/ai-review", json={})
    assert resp.status_code == 404


def test_ai_review_trade_not_owned(asgi_client, login_as, premium_user_dict):
    """Trade owned by another user → 404 (ownership check)."""
    with login_as(premium_user_dict):
        with patch("carrac.content_db.get_content_db") as mock_factory:
            mock_db = MagicMock()
            mock_db.get_journal_entry.return_value = {
                "id": 5, "user_id": 999, "asset": "BTC", "closed_at": "2026-05-01",
            }
            mock_factory.return_value = mock_db
            resp = asgi_client.post("/api/journal/trades/5/ai-review", json={})
    assert resp.status_code == 404


def test_ai_review_trade_not_closed(asgi_client, login_as, premium_user_dict):
    """Trade still open (no closed_at) → 400."""
    with login_as(premium_user_dict):
        with patch("carrac.content_db.get_content_db") as mock_factory:
            mock_db = MagicMock()
            mock_db.get_journal_entry.return_value = {
                "id": 5, "user_id": 42, "asset": "BTC", "closed_at": None,
            }
            mock_factory.return_value = mock_db
            resp = asgi_client.post("/api/journal/trades/5/ai-review", json={})
    assert resp.status_code == 400


def test_ai_review_trade_happy_path(asgi_client, login_as, premium_user_dict):
    """Closed trade owned by authenticated user → 200 with review dict."""
    review_result = {
        "review": "Strong setup, good execution.",
        "grade": {"setup": 8, "execution": 7, "exit": 6, "discipline": 9},
        "suggested_mistakes": [],
        "charged": 5,
    }
    with login_as(premium_user_dict):
        with patch("carrac.content_db.get_content_db") as mock_factory:
            mock_db = MagicMock()
            mock_db.get_journal_entry.return_value = {
                "id": 5, "user_id": 42, "asset": "BTC", "closed_at": "2026-05-01",
            }
            mock_factory.return_value = mock_db
            with patch("carrac.journal.ai_review.review_trade", return_value=review_result):
                resp = asgi_client.post("/api/journal/trades/5/ai-review", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert "review" in body
    assert "grade" in body


def test_ai_session_summary_not_owned(asgi_client, login_as, premium_user_dict):
    """Session owned by another user → 404."""
    with login_as(premium_user_dict):
        with patch("carrac.content_db.get_content_db") as mock_factory:
            mock_db = MagicMock()
            mock_db.get_session.return_value = {"id": 3, "user_id": 999}
            mock_factory.return_value = mock_db
            resp = asgi_client.post("/api/journal/sessions/3/ai-summary", json={})
    assert resp.status_code == 404


def test_ai_session_summary_happy_path(asgi_client, login_as, premium_user_dict):
    """Owned session → 200 with summary dict."""
    summary_result = {
        "summary": "Disciplined session. 2 trades, 1 winner.",
        "themes": ["Patience"],
        "rule_adherence_pct": 90,
        "key_observation": "Both entries came after confirmation.",
        "pattern_to_watch": "Entries against the 1H trend.",
        "charged": 5,
    }
    with login_as(premium_user_dict):
        with patch("carrac.content_db.get_content_db") as mock_factory:
            mock_db = MagicMock()
            mock_db.get_session.return_value = {"id": 3, "user_id": 42}
            mock_factory.return_value = mock_db
            with patch("carrac.journal.ai_review.summarize_session", return_value=summary_result):
                resp = asgi_client.post("/api/journal/sessions/3/ai-summary", json={})
    assert resp.status_code == 200
    assert "summary" in resp.json()


def test_weekly_journal_review_days_out_of_range(asgi_client, login_as, premium_user_dict):
    """``days`` below minimum (7) → 400."""
    with login_as(premium_user_dict):
        resp = asgi_client.post(
            "/api/journal/ai-review/weekly",
            json={"days": 3},
        )
    assert resp.status_code == 400


def test_weekly_journal_review_happy_path(asgi_client, login_as, premium_user_dict):
    """Happy path: weekly review returns 200 JSON dict."""
    weekly_result = {"review": "Good week.", "stats": {}, "charged": 7}
    with login_as(premium_user_dict):
        with patch("carrac.content_db.get_content_db") as mock_factory:
            mock_db = MagicMock()
            mock_factory.return_value = mock_db
            with patch("carrac.journal.ai_review.weekly_review", return_value=weekly_result):
                resp = asgi_client.post(
                    "/api/journal/ai-review/weekly",
                    json={"days": 14},
                )
    assert resp.status_code == 200
    assert "review" in resp.json()


def test_ai_query_sse_happy_path(asgi_client, login_as, premium_user_dict):
    """Valid question → SSE stream with chunks."""

    def _fake_nl_stream(db, user_id, question):
        yield 'data: {"t": "The answer:"}\n\n'
        yield 'data: {"done": true}\n\n'

    with login_as(premium_user_dict):
        with patch("carrac.content_db.get_content_db") as mock_factory:
            mock_db = MagicMock()
            mock_factory.return_value = mock_db
            with patch("carrac.journal.ai_review.nl_query_stream", side_effect=_fake_nl_stream):
                resp = asgi_client.post(
                    "/api/journal/ai-query",
                    json={"question": "Which asset has the most winning trades?"},
                )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    raw = resp.text
    assert 'data: ' in raw
    assert '"t":' in raw
    assert '"done": true' in raw or '"done":true' in raw
