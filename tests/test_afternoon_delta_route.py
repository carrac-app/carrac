"""GET /api/report/afternoon/latest must include `delta`.

Two scenarios:
  1. Report whose metadata contains a delta dict → response has `delta` with
     all expected sub-keys (regime, fear_greed, signal_flips, new_prints).
  2. Report with NO metadata / no delta key → response has `delta: null`.
"""

from __future__ import annotations

import json



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_DELTA = {
    "regime": {"was": "BULL", "now": "BULL", "changed": False},
    "fear_greed": {"was": 55, "now": 60, "dir": "up"},
    "levels_tested": [],
    "signal_flips": [{"asset": "BTC", "from": "neutral", "to": "bullish"}],
    "new_prints": [{"name": "CPI", "actual": 3.1, "forecast": 3.2}],
}


def _seed_afternoon(app, *, with_delta: bool) -> int:
    """Insert an afternoon report into the test DB; returns the report id."""
    from carrac.content_db import get_content_db
    metadata_str = json.dumps({"delta": _SAMPLE_DELTA}) if with_delta else None
    return get_content_db().save_report(
        content="Afternoon update content.",
        assets_included=[],
        generation_time_ms=10,
        report_type="afternoon",
        metadata=metadata_str,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAfternoonDeltaRoute:
    """GET /api/report/afternoon/latest includes delta from metadata."""

    def _admin_client(self, client):
        with client.session_transaction() as sess:
            sess["user_id"] = "admin"
            sess["email"] = "admin@example.com"
            sess["is_admin"] = True
            sess["tier"] = "admin"
        return client

    # -- delta present -------------------------------------------------------

    def test_delta_present_when_metadata_has_delta(self, client, app):
        """When metadata contains a delta dict, the endpoint returns it."""
        _seed_afternoon(app, with_delta=True)
        c = self._admin_client(client)
        resp = c.get("/api/report/afternoon/latest")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "delta" in data, "Response must include a 'delta' key"
        delta = data["delta"]
        assert delta is not None, "delta must not be null when metadata has delta"
        assert "regime" in delta
        assert "fear_greed" in delta
        assert "signal_flips" in delta
        assert "new_prints" in delta

    def test_delta_sub_keys_match_persisted_values(self, client, app):
        """delta sub-key values must round-trip from metadata correctly."""
        _seed_afternoon(app, with_delta=True)
        c = self._admin_client(client)
        data = c.get("/api/report/afternoon/latest").get_json()
        delta = data["delta"]
        assert delta["regime"]["now"] == "BULL"
        assert delta["fear_greed"]["dir"] == "up"
        assert len(delta["signal_flips"]) == 1
        assert delta["signal_flips"][0]["asset"] == "BTC"
        assert len(delta["new_prints"]) == 1
        assert delta["new_prints"][0]["name"] == "CPI"

    # -- delta absent --------------------------------------------------------

    def test_delta_null_when_no_metadata(self, client, app):
        """When the report has no metadata, delta must be null (no 500)."""
        _seed_afternoon(app, with_delta=False)
        c = self._admin_client(client)
        resp = c.get("/api/report/afternoon/latest")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "delta" in data, "Response must always include 'delta' key"
        assert data["delta"] is None

    # -- existing keys untouched ---------------------------------------------

    def test_existing_keys_still_present(self, client, app):
        """Adding delta must not remove content, created_at, or id."""
        _seed_afternoon(app, with_delta=True)
        c = self._admin_client(client)
        data = c.get("/api/report/afternoon/latest").get_json()
        assert "content" in data
        assert "created_at" in data
        assert "id" in data

    def test_returns_200_always(self, client, app):
        """The endpoint returns 200 even when no afternoon report exists yet."""
        c = self._admin_client(client)
        resp = c.get("/api/report/afternoon/latest")
        assert resp.status_code == 200
