"""Alerts writes routing tests.

Exercises the strangler dispatcher to confirm POST /api/alerts and its
sub-paths, plus PUT/DELETE /api/alerts/<id> flip to the FastAPI runtime
while all other method+path combinations stay on Flask.

Pydantic ``extra="forbid"`` shape errors surface as 400 with the canonical
error envelope. Deep semantic validation (SSRF, indicator whitelist, formula
parse) stays in the Flask helpers; those are unit-tested separately.
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
# Dispatcher shim unit tests — no network, no app context.
# ---------------------------------------------------------------------------


def test_post_alerts_exact_path_matches_post_only():
    ep = ExactPath("/api/alerts", app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": "/api/alerts"}

    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})
    put_match, _ = ep.matches({**base, "method": "PUT"})

    assert post_match.value > 0, "POST should match"
    assert get_match.value == 0, "GET should not match (stays on Flask)"
    assert put_match.value == 0, "PUT should not match"


def test_alerts_id_prefix_matches_put_delete_only():
    pp = PrefixPath("/api/alerts/", app=None, methods=frozenset({"PUT", "DELETE"}))
    base = {"type": "http", "path": "/api/alerts/42"}

    put_match, _ = pp.matches({**base, "method": "PUT"})
    del_match, _ = pp.matches({**base, "method": "DELETE"})
    get_match, _ = pp.matches({**base, "method": "GET"})
    post_match, _ = pp.matches({**base, "method": "POST"})

    assert put_match.value > 0, "PUT /api/alerts/42 should match"
    assert del_match.value > 0, "DELETE /api/alerts/42 should match"
    assert get_match.value == 0, "GET /api/alerts/42 should not match"
    assert post_match.value == 0, "POST /api/alerts/42 should not match"


def test_alert_subtype_posts_do_not_collide_with_prefix_put_delete():
    """POST /api/alerts/trendline must NOT be captured by the PUT/DELETE prefix."""
    pp = PrefixPath("/api/alerts/", app=None, methods=frozenset({"PUT", "DELETE"}))
    scope = {"type": "http", "path": "/api/alerts/trendline", "method": "POST"}
    match, _ = pp.matches(scope)
    assert match.value == 0, "POST on subpath must not match PUT/DELETE prefix"


@pytest.mark.parametrize(
    "path",
    [
        "/api/alerts",
        "/api/alerts/trendline",
        "/api/alerts/indicator",
        "/api/alerts/formula",
        "/api/alerts/zscore",
        "/api/alerts/regime",
        "/api/alerts/chain_strength",
        "/api/alerts/anomaly_cluster",
        "/api/alerts/conditional",
        "/api/notification-settings",
        "/api/user/analyst-sources",
    ],
)
def test_alert_paths_listed_in_migrated_write_paths(path):
    entries = {p for p, _ in MIGRATED_WRITE_PATHS}
    assert path in entries, f"{path} not in MIGRATED_WRITE_PATHS"


def test_alerts_prefix_listed_in_migrated_write_prefixes():
    assert ("/api/alerts/", frozenset({"PUT", "DELETE"})) in MIGRATED_WRITE_PREFIXES


def test_notification_settings_post_is_exact_path():
    ep = ExactPath("/api/notification-settings", app=None, methods=frozenset({"POST"}))
    match, _ = ep.matches(
        {"type": "http", "path": "/api/notification-settings", "method": "POST"}
    )
    assert match.value > 0


# ---------------------------------------------------------------------------
# Pydantic model shape tests — extra="forbid" 400 cases.
# ---------------------------------------------------------------------------


def test_alert_create_request_rejects_unknown_fields():
    from carrac.fastapi_app.models.alerts import AlertCreateRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AlertCreateRequest(
            symbol="BTC",
            condition="above",
            price=100000.0,
            evil_field="injection",
        )


def test_alert_create_request_alias_resolution():
    from carrac.fastapi_app.models.alerts import AlertCreateRequest

    req = AlertCreateRequest(
        asset="BTCUSDT",
        direction="below",
        price=90000.0,
    )
    assert req.symbol == "BTCUSDT"
    assert req.condition == "below"


def test_alert_create_request_bad_condition_raises():
    from carrac.fastapi_app.models.alerts import AlertCreateRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AlertCreateRequest(symbol="BTC", condition="sideways", price=100.0)


def test_trendline_alert_request_rejects_identical_timestamps():
    from carrac.fastapi_app.models.alerts import TrendlineAlertRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TrendlineAlertRequest(
            symbol="BTC",
            drawing_id=1,
            p1_ts=1000.0,
            p1_val=50000.0,
            p2_ts=1000.0,  # same as p1_ts
            p2_val=55000.0,
        )


def test_zscore_alert_request_rejects_negative_threshold():
    from carrac.fastapi_app.models.alerts import ZscoreAlertRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ZscoreAlertRequest(metric="NUPL", condition="above", threshold=-1.0)


def test_notification_settings_request_extra_forbidden():
    from carrac.fastapi_app.models.alerts import NotificationSettingsRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NotificationSettingsRequest(
            ntfy={"enabled": False, "url": ""},
            unknown_channel={"enabled": True},
        )


def test_conditional_alert_normalises_string_conditions():
    from carrac.fastapi_app.models.alerts import ConditionalAlertRequest

    req = ConditionalAlertRequest(conditions="NUPL > 0.5, MVRV < 3.0")
    assert isinstance(req.conditions, list)
    assert len(req.conditions) == 2


def test_analyst_sources_request_extra_forbidden():
    from carrac.fastapi_app.models.alerts import AnalystSourcesRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnalystSourcesRequest(x_handles=["user1"], yt_channels={}, rogue_field=True)


def test_alert_update_request_extra_forbidden():
    from carrac.fastapi_app.models.alerts import AlertUpdateRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AlertUpdateRequest(price=50000.0, sneaky_extra="bad")
