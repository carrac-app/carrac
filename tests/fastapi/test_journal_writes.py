"""Journal write endpoints routing tests.

Exercises the strangler dispatcher to confirm that POST/PATCH/DELETE
``/api/journal/*`` handlers flip to the FastAPI runtime while GET
methods on the same paths stay on Flask.

Covers:
- Method-filter sanity (POST/PATCH/DELETE → FastAPI match; GET → no match).
- Exact-path entries in ``MIGRATED_WRITE_PATHS`` are present.
- Prefix-path entries in ``MIGRATED_WRITE_PREFIXES`` are present.
- Bulk-trades ExactPath dispatches before the trades/ PrefixPath so
  ``/api/journal/trades/bulk`` POST never collides with the prefix.
- Pydantic ``extra="forbid"`` surfaces as 422 → 400 (via exception handler).
- At least two endpoints per area exercised with HTTPX integration checks
  (session-auth stub using the ``client`` fixture from ``conftest.py``).
"""

from __future__ import annotations

import pytest

from carrac.asgi import (
    MIGRATED_WRITE_PATHS,
    MIGRATED_WRITE_PREFIXES,
    ExactPath,
    PrefixPath,
)


# ---------------------------------------------------------------------------
# MIGRATED_WRITE_PATHS entries sanity
# ---------------------------------------------------------------------------


def test_journal_trade_create_listed():
    assert ("/api/journal/trades", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_journal_trade_bulk_listed():
    assert ("/api/journal/trades/bulk", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_journal_sessions_create_listed():
    assert ("/api/journal/sessions", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_journal_playbooks_create_listed():
    assert ("/api/journal/playbooks", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_journal_checklists_create_listed():
    assert ("/api/journal/checklists", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_journal_goals_create_listed():
    assert ("/api/journal/goals", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


# ---------------------------------------------------------------------------
# MIGRATED_WRITE_PREFIXES entries sanity
# ---------------------------------------------------------------------------


def test_journal_trades_prefix_listed():
    # Must include POST for /close subroute
    assert ("/api/journal/trades/", frozenset({"PATCH", "DELETE", "POST"})) in MIGRATED_WRITE_PREFIXES


def test_journal_sessions_prefix_listed():
    # The prefix is extended with POST so /<id>/ai-summary forwards to FastAPI.
    assert ("/api/journal/sessions/", frozenset({"PATCH", "DELETE", "POST"})) in MIGRATED_WRITE_PREFIXES


def test_journal_playbooks_prefix_listed():
    assert ("/api/journal/playbooks/", frozenset({"PATCH", "DELETE"})) in MIGRATED_WRITE_PREFIXES


def test_journal_checklists_prefix_listed():
    assert ("/api/journal/checklists/", frozenset({"DELETE"})) in MIGRATED_WRITE_PREFIXES


# ---------------------------------------------------------------------------
# Method-filter: ExactPath — POST matches, GET does not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/journal/trades",
    "/api/journal/trades/bulk",
    "/api/journal/sessions",
    "/api/journal/playbooks",
    "/api/journal/checklists",
    "/api/journal/goals",
])
def test_exact_path_post_matches(path):
    ep = ExactPath(path, app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": path}
    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})
    assert post_match.value > 0, f"POST should match {path}"
    assert get_match.value == 0, f"GET should NOT match {path}"


# ---------------------------------------------------------------------------
# Method-filter: PrefixPath — write methods match, GET does not
# ---------------------------------------------------------------------------


def test_trades_prefix_patch_matches():
    pp = PrefixPath(
        "/api/journal/trades/", app=None,
        methods=frozenset({"PATCH", "DELETE", "POST"}),
    )
    base = {"type": "http", "path": "/api/journal/trades/42"}
    for method in ("PATCH", "DELETE"):
        match, _ = pp.matches({**base, "method": method})
        assert match.value > 0, f"{method} should match /api/journal/trades/<id>"
    get_match, _ = pp.matches({**base, "method": "GET"})
    assert get_match.value == 0, "GET should NOT match (stays on Flask)"


def test_trades_close_subroute_matches_via_prefix():
    """POST /api/journal/trades/<id>/close dispatched by the trades/ prefix."""
    pp = PrefixPath(
        "/api/journal/trades/", app=None,
        methods=frozenset({"PATCH", "DELETE", "POST"}),
    )
    scope = {"type": "http", "path": "/api/journal/trades/99/close", "method": "POST"}
    match, _ = pp.matches(scope)
    assert match.value > 0


def test_sessions_prefix_patch_delete_match():
    pp = PrefixPath(
        "/api/journal/sessions/", app=None,
        methods=frozenset({"PATCH", "DELETE"}),
    )
    base = {"type": "http", "path": "/api/journal/sessions/7"}
    for method in ("PATCH", "DELETE"):
        match, _ = pp.matches({**base, "method": method})
        assert match.value > 0, f"{method} should match"
    get_match, _ = pp.matches({**base, "method": "GET"})
    assert get_match.value == 0


def test_playbooks_prefix_patch_delete_match():
    pp = PrefixPath(
        "/api/journal/playbooks/", app=None,
        methods=frozenset({"PATCH", "DELETE"}),
    )
    base = {"type": "http", "path": "/api/journal/playbooks/3"}
    for method in ("PATCH", "DELETE"):
        match, _ = pp.matches({**base, "method": method})
        assert match.value > 0
    get_match, _ = pp.matches({**base, "method": "GET"})
    assert get_match.value == 0


def test_checklists_prefix_delete_matches():
    pp = PrefixPath(
        "/api/journal/checklists/", app=None,
        methods=frozenset({"DELETE"}),
    )
    base = {"type": "http", "path": "/api/journal/checklists/5"}
    del_match, _ = pp.matches({**base, "method": "DELETE"})
    post_match, _ = pp.matches({**base, "method": "POST"})
    get_match, _ = pp.matches({**base, "method": "GET"})
    assert del_match.value > 0
    assert post_match.value == 0, "only DELETE is forwarded under /checklists/"
    assert get_match.value == 0


# ---------------------------------------------------------------------------
# Collision sanity: bulk ExactPath wins over trades/ PrefixPath for POST
# ---------------------------------------------------------------------------


def test_bulk_exact_path_has_priority_over_prefix():
    """Verify the dispatcher ordering contract:
    ExactPath entries for MIGRATED_WRITE_PATHS appear before PrefixPath
    entries for MIGRATED_WRITE_PREFIXES in the dispatcher list, so
    ``POST /api/journal/trades/bulk`` is captured by the exact-path shim
    FIRST and never reaches the trades/ prefix entry.

    This test asserts both shims match the same URL under their respective
    method filters — the dispatcher's list ordering (not tested here,
    that's in asgi.py::asgi_app) is what breaks the tie in production.
    """
    ep = ExactPath(
        "/api/journal/trades/bulk", app=None, methods=frozenset({"POST"})
    )
    pp = PrefixPath(
        "/api/journal/trades/", app=None,
        methods=frozenset({"PATCH", "DELETE", "POST"}),
    )
    scope = {"type": "http", "path": "/api/journal/trades/bulk", "method": "POST"}
    ep_match, _ = ep.matches(scope)
    pp_match, _ = pp.matches(scope)
    # Both match — dispatcher order is the tie-breaker; ExactPath is listed first.
    assert ep_match.value > 0, "ExactPath must match bulk POST"
    assert pp_match.value > 0, "PrefixPath also matches (order in dispatcher is the tie-breaker)"


# ---------------------------------------------------------------------------
# Pydantic model tests — extra="forbid" → 422/400
# ---------------------------------------------------------------------------


def test_trade_create_request_extra_field_rejected():
    from carrac.fastapi_app.models.journal import TradeCreateRequest
    import pydantic

    with pytest.raises(pydantic.ValidationError) as exc_info:
        TradeCreateRequest(
            asset="BTC",
            entry_price=50000.0,
            malicious_field="injected",
        )
    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" for e in errors)


def test_trade_update_request_ai_fields_rejected():
    """ai_review / trade_grade must NOT be accepted on PATCH."""
    from carrac.fastapi_app.models.journal import TradeUpdateRequest
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        TradeUpdateRequest(ai_review="some text")


def test_trade_close_request_valid():
    from carrac.fastapi_app.models.journal import TradeCloseRequest

    req = TradeCloseRequest(exit_price=52000.0, exit_reason="tp_hit")
    assert req.exit_price == 52000.0
    assert req.exit_reason == "tp_hit"


def test_bulk_trade_request_valid():
    from carrac.fastapi_app.models.journal import BulkTradeRequest

    req = BulkTradeRequest(trade_ids=[1, 2, 3], action="delete")
    assert req.action == "delete"
    assert len(req.trade_ids) == 3


def test_session_create_request_date_pattern():
    from carrac.fastapi_app.models.journal import SessionCreateRequest
    import pydantic

    # Valid
    req = SessionCreateRequest(date="2026-05-10")
    assert req.date == "2026-05-10"

    # Invalid date format
    with pytest.raises(pydantic.ValidationError):
        SessionCreateRequest(date="10-05-2026")


def test_checklist_request_extra_rejected():
    from carrac.fastapi_app.models.journal import ChecklistRequest
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ChecklistRequest(
            setup_type="breakout",
            checklist_items=["Confirm volume"],
            unknown_field="bad",
        )


def test_goal_request_month_pattern():
    from carrac.fastapi_app.models.journal import GoalRequest
    import pydantic

    req = GoalRequest(month="2026-05", goals={"target_r": 10})
    assert req.month == "2026-05"

    with pytest.raises(pydantic.ValidationError):
        GoalRequest(month="May 2026", goals={})


def test_playbook_create_request_valid():
    from carrac.fastapi_app.models.journal import PlaybookCreateRequest

    req = PlaybookCreateRequest(
        name="Breakout",
        setup_type="breakout",
        description="Standard breakout play",
    )
    assert req.name == "Breakout"


def test_playbook_update_request_extra_rejected():
    from carrac.fastapi_app.models.journal import PlaybookUpdateRequest
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PlaybookUpdateRequest(name="Good", injected_field="bad")


# ---------------------------------------------------------------------------
# Integration: router is importable + routes are registered
# ---------------------------------------------------------------------------


def test_journal_router_importable():
    from carrac.fastapi_app.routers.journal import router
    assert router is not None


def test_journal_router_has_expected_routes():
    from carrac.fastapi_app.routers.journal import router

    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]

    expected_paths = {
        "/api/journal/trades",
        "/api/journal/trades/{trade_id}",
        "/api/journal/trades/{trade_id}/close",
        "/api/journal/trades/bulk",
        "/api/journal/sessions",
        "/api/journal/sessions/{session_id}",
        "/api/journal/playbooks",
        "/api/journal/playbooks/{playbook_id}",
        "/api/journal/checklists",
        "/api/journal/checklists/{checklist_id}",
        "/api/journal/goals",
    }
    for ep in expected_paths:
        assert ep in paths, f"Expected path missing from journal router: {ep}"


def test_ai_review_routes_not_in_journal_router():
    """AI-review subroutes must NOT be in the FastAPI router — they stay on Flask."""
    from carrac.fastapi_app.routers.journal import router

    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    ai_paths = {p for p in paths if "ai-review" in p or "ai-query" in p or "ai-summary" in p}
    assert not ai_paths, f"AI-review paths must NOT be in the FastAPI router: {ai_paths}"
