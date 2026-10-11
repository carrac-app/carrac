"""Tests for trigger (A): the scheduled daily social card pipeline.

carrac/social/daily_card.py assembles a candidate (grounded caption + chart
screenshot); carrac/social/postiz_client.py stages it into Postiz as a DRAFT,
dry-run by default. No real network call is ever exercised in this suite —
report/DB access and the screenshot + Postiz HTTP calls are all monkeypatched,
several tests assert on it by making the underlying call raise if invoked.
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from carrac.social import daily_card, postiz_client


# ---------------------------------------------------------------------------
# Shared fakes (mirrors tests/fastapi/test_reports_x_candidates.py's _FakeDB —
# same underlying store the live /api/report/morning/latest handler reads).
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def get_latest_report(self, report_type=None):
        return self._rows.get(report_type)


def _general_row(candidates=None, report_id=42):
    meta = json.dumps({"x_candidates": candidates}) if candidates is not None else None
    return {
        "id": report_id,
        "content": "## The Story\nNeutral overview.\n",
        "created_at": "2026-07-17T09:00:00+00:00",
        "metadata": meta,
    }


def _patch_db(monkeypatch, rows):
    from carrac import content_db as cdb
    monkeypatch.setattr(cdb, "get_content_db", lambda: _FakeDB(rows))


def _raise(*_a, **_kw):
    raise AssertionError("network call must not happen here")


# ---------------------------------------------------------------------------
# fetch_first_x_candidate
# ---------------------------------------------------------------------------

class TestFetchFirstXCandidate:
    def test_returns_first_candidate(self, monkeypatch):
        _patch_db(monkeypatch, {"general": _general_row(["First tweet", "Second tweet"])})
        assert daily_card.fetch_first_x_candidate() == "First tweet"

    def test_no_general_report_returns_none_and_logs(self, monkeypatch, caplog):
        _patch_db(monkeypatch, {})
        with caplog.at_level(logging.WARNING):
            assert daily_card.fetch_first_x_candidate() is None
        assert "no general report" in caplog.text

    def test_empty_x_candidates_returns_none_and_logs(self, monkeypatch, caplog):
        _patch_db(monkeypatch, {"general": _general_row([])})
        with caplog.at_level(logging.WARNING):
            assert daily_card.fetch_first_x_candidate() is None
        assert "no x_candidates" in caplog.text

    def test_missing_metadata_returns_none(self, monkeypatch):
        _patch_db(monkeypatch, {"general": _general_row(candidates=None)})
        assert daily_card.fetch_first_x_candidate() is None

    def test_blank_first_candidate_returns_none(self, monkeypatch):
        _patch_db(monkeypatch, {"general": _general_row(["   ", "Second tweet"])})
        assert daily_card.fetch_first_x_candidate() is None


# ---------------------------------------------------------------------------
# capture_chart_screenshot
# ---------------------------------------------------------------------------

class TestCaptureChartScreenshot:
    def test_writes_bytes_and_returns_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SCREENSHOT_API_KEY", "test-key")

        class _FakeResp:
            status_code = 200
            headers = {"Content-Type": "image/jpeg"}
            content = b"\xff\xd8fakejpeg"

        def _fake_get(url, params=None, headers=None, timeout=None):
            assert "/api/screenshot/BTC/1D" in url
            assert headers["X-Internal-Key"] == "test-key"
            return _FakeResp()

        monkeypatch.setattr(daily_card.requests, "get", _fake_get)
        path = daily_card.capture_chart_screenshot()
        assert path is not None
        assert os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read() == b"\xff\xd8fakejpeg"
        assert path.startswith(str(tmp_path))

    def test_missing_api_key_returns_none_no_network(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("SCREENSHOT_API_KEY", raising=False)
        monkeypatch.setattr(daily_card.requests, "get", _raise)
        assert daily_card.capture_chart_screenshot() is None

    def test_invalid_asset_returns_none_no_network(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SCREENSHOT_API_KEY", "test-key")
        monkeypatch.setattr(daily_card.requests, "get", _raise)
        assert daily_card.capture_chart_screenshot(asset="../../etc/passwd") is None

    def test_non_200_response_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SCREENSHOT_API_KEY", "test-key")

        class _FakeResp:
            status_code = 503
            headers = {"Content-Type": "application/json"}
            content = b"{}"

        monkeypatch.setattr(daily_card.requests, "get", lambda *a, **kw: _FakeResp())
        assert daily_card.capture_chart_screenshot() is None


# ---------------------------------------------------------------------------
# assemble_daily_candidate / persist_candidate
# ---------------------------------------------------------------------------

class TestAssembleDailyCandidate:
    def test_returns_pending_candidate_dict(self, monkeypatch):
        monkeypatch.setattr(daily_card, "fetch_first_x_candidate", lambda: "Grounded caption")
        monkeypatch.setattr(
            daily_card, "capture_chart_screenshot",
            lambda asset, timeframe: "/tmp/fake.jpg",
        )
        candidate = daily_card.assemble_daily_candidate()
        assert candidate == {
            "caption": "Grounded caption",
            "image_path": "/tmp/fake.jpg",
            "asset": daily_card.DEFAULT_ASSET,
            "created_at": candidate["created_at"],
            "status": "pending",
        }
        assert candidate["created_at"]  # non-empty ISO timestamp

    def test_no_caption_returns_none_and_skips_screenshot(self, monkeypatch):
        monkeypatch.setattr(daily_card, "fetch_first_x_candidate", lambda: None)
        monkeypatch.setattr(daily_card, "capture_chart_screenshot", _raise)
        assert daily_card.assemble_daily_candidate() is None

    def test_no_candidates_end_to_end_via_fake_db(self, monkeypatch, caplog):
        """Full fallback path: empty x_candidates -> no candidate created at all."""
        _patch_db(monkeypatch, {"general": _general_row([])})
        monkeypatch.setattr(daily_card, "capture_chart_screenshot", _raise)
        with caplog.at_level(logging.WARNING):
            assert daily_card.assemble_daily_candidate() is None
        assert "no x_candidates" in caplog.text


class TestPersistCandidate:
    def test_writes_json_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        candidate = {
            "caption": "x", "image_path": None, "asset": "BTC",
            "created_at": "2026-07-17T09:00:00+00:00", "status": "pending",
        }
        path = daily_card.persist_candidate(candidate)
        assert os.path.exists(path)
        with open(path) as f:
            assert json.load(f) == candidate


# ---------------------------------------------------------------------------
# postiz_client: dry-run default + draft-only guard
# ---------------------------------------------------------------------------

class TestIsDryRun:
    def test_default_dry_run_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("POSTIZ_URL", raising=False)
        monkeypatch.delenv("POSTIZ_API_KEY", raising=False)
        monkeypatch.delenv("SOCIAL_DRY_RUN", raising=False)
        assert postiz_client.is_dry_run() is True

    def test_dry_run_even_when_configured_if_not_opted_in(self, monkeypatch):
        monkeypatch.setenv("POSTIZ_URL", "http://postiz.local")
        monkeypatch.setenv("POSTIZ_API_KEY", "key")
        monkeypatch.delenv("SOCIAL_DRY_RUN", raising=False)
        assert postiz_client.is_dry_run() is True  # SOCIAL_DRY_RUN unset defaults to dry-run

    def test_live_only_when_fully_configured_and_opted_in(self, monkeypatch):
        monkeypatch.setenv("POSTIZ_URL", "http://postiz.local")
        monkeypatch.setenv("POSTIZ_API_KEY", "key")
        monkeypatch.setenv("SOCIAL_DRY_RUN", "0")
        assert postiz_client.is_dry_run() is False


class TestStageCandidateDryRun:
    def test_no_network_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("POSTIZ_URL", raising=False)
        monkeypatch.delenv("POSTIZ_API_KEY", raising=False)
        monkeypatch.delenv("SOCIAL_DRY_RUN", raising=False)
        monkeypatch.setattr(postiz_client.requests, "post", _raise)
        monkeypatch.setattr(postiz_client.requests, "get", _raise)

        candidate = {"caption": "hello", "image_path": None, "asset": "BTC"}
        result = postiz_client.stage_candidate(candidate)
        assert result["status"] == "dry_run"

    def test_forced_dry_run_ignores_full_config(self, monkeypatch):
        monkeypatch.setenv("POSTIZ_URL", "http://postiz.local")
        monkeypatch.setenv("POSTIZ_API_KEY", "key")
        monkeypatch.setenv("SOCIAL_DRY_RUN", "0")
        monkeypatch.setattr(postiz_client.requests, "post", _raise)

        result = postiz_client.stage_candidate({"caption": "hello"}, dry_run=True)
        assert result["status"] == "dry_run"

    def test_live_path_without_integration_id_raises_before_network(self, monkeypatch):
        monkeypatch.setenv("POSTIZ_URL", "http://postiz.local")
        monkeypatch.setenv("POSTIZ_API_KEY", "key")
        monkeypatch.delenv("POSTIZ_X_INTEGRATION_ID", raising=False)
        monkeypatch.setattr(postiz_client.requests, "post", _raise)

        with pytest.raises(RuntimeError):
            postiz_client.stage_candidate({"caption": "x"}, dry_run=False)


class TestDraftOnlyGuard:
    def test_non_draft_post_type_raises(self):
        with pytest.raises(ValueError):
            postiz_client._build_post_payload({"caption": "x"}, "int-1", post_type="now")
        with pytest.raises(ValueError):
            postiz_client._build_post_payload({"caption": "x"}, "int-1", post_type="schedule")
        with pytest.raises(ValueError):
            postiz_client._build_post_payload({"caption": "x"}, "int-1", post_type="publish")

    def test_draft_payload_shape(self):
        payload = postiz_client._build_post_payload({"caption": "hello world"}, "int-1")
        assert payload["type"] == "draft"
        assert payload["posts"][0]["integration"]["id"] == "int-1"
        assert payload["posts"][0]["value"][0]["content"] == "hello world"

    def test_x_settings_include_who_can_reply_post(self):
        """Live-verified: Postiz 400s on X posts without this."""
        payload = postiz_client._build_post_payload({"caption": "hello world"}, "int-1")
        settings = payload["posts"][0]["settings"]
        assert settings["__type"] == "x"
        assert settings["who_can_reply_post"] == "everyone"

    def test_value_has_exactly_two_entries_link_only_in_second(self, monkeypatch):
        monkeypatch.setenv("SITE_URL", "https://example.test")
        monkeypatch.delenv("SOCIAL_LINK_URL", raising=False)
        """Live 2026-07-17 finding: link-in-first-comment — main tweet has NO link."""
        candidate = {"caption": "hello world, no link here", "created_at": "2026-07-17T09:00:00+00:00"}
        payload = postiz_client._build_post_payload(candidate, "int-1")
        value = payload["posts"][0]["value"]
        assert len(value) == 2
        assert value[0]["content"] == "hello world, no link here"
        assert "http" not in value[0]["content"]
        assert "example.test" in value[1]["content"]
        assert "carrac.cc" not in value[1]["content"]
        assert value[1]["content"].count("http") == 1

    def test_link_comment_uses_default_base_url_and_campaign_date(self, monkeypatch):
        monkeypatch.delenv("SOCIAL_LINK_URL", raising=False)
        monkeypatch.setenv("SITE_URL", "https://example.test")
        candidate = {"caption": "x", "created_at": "2026-07-17T09:00:00+00:00"}
        payload = postiz_client._build_post_payload(candidate, "int-1")
        link_content = payload["posts"][0]["value"][1]["content"]
        assert "carrac.cc" not in link_content
        assert "https://example.test/?utm_source=x&utm_medium=social&utm_campaign=daily-2026-07-17" in link_content

    def test_link_comment_respects_social_link_url_override(self, monkeypatch):
        monkeypatch.setenv("SOCIAL_LINK_URL", "https://example.test/")
        candidate = {"caption": "x", "created_at": "2026-07-17T09:00:00+00:00"}
        payload = postiz_client._build_post_payload(candidate, "int-1")
        link_content = payload["posts"][0]["value"][1]["content"]
        assert link_content.startswith("More context on this read")
        assert "https://example.test/?utm_source=x&utm_medium=social&utm_campaign=daily-2026-07-17" in link_content
        assert "carrac.cc" not in link_content

    def test_link_comment_falls_back_to_today_when_created_at_missing(self, monkeypatch):
        monkeypatch.delenv("SOCIAL_LINK_URL", raising=False)
        today = postiz_client.datetime.now(postiz_client.timezone.utc).date().isoformat()
        payload = postiz_client._build_post_payload({"caption": "x"}, "int-1")
        link_content = payload["posts"][0]["value"][1]["content"]
        assert f"utm_campaign=daily-{today}" in link_content

    def test_link_comment_falls_back_to_today_when_created_at_unparseable(self, monkeypatch):
        monkeypatch.delenv("SOCIAL_LINK_URL", raising=False)
        today = postiz_client.datetime.now(postiz_client.timezone.utc).date().isoformat()
        candidate = {"caption": "x", "created_at": "not-a-date"}
        payload = postiz_client._build_post_payload(candidate, "int-1")
        link_content = payload["posts"][0]["value"][1]["content"]
        assert f"utm_campaign=daily-{today}" in link_content

    def test_create_draft_post_only_ever_sends_draft(self, monkeypatch):
        """Even the real network path (mocked here) must post type=draft."""
        monkeypatch.setenv("POSTIZ_URL", "http://postiz.local")
        monkeypatch.setenv("POSTIZ_API_KEY", "key")

        captured = {}

        class _FakeResp:
            content = b'{"id": "post-1"}'

            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "post-1"}

        def _fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResp()

        monkeypatch.setattr(postiz_client.requests, "post", _fake_post)
        result = postiz_client._create_draft_post({"caption": "hi", "image_path": None}, "int-1")
        assert captured["json"]["type"] == "draft"
        assert result["status"] == "draft"


# ---------------------------------------------------------------------------
# run() — full CLI-entry pipeline, no network in dry-run
# ---------------------------------------------------------------------------

class TestRunEndToEnd:
    def test_dry_run_full_pipeline_no_network(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("POSTIZ_URL", raising=False)
        monkeypatch.delenv("POSTIZ_API_KEY", raising=False)
        monkeypatch.delenv("SCREENSHOT_API_KEY", raising=False)

        _patch_db(monkeypatch, {"general": _general_row(["Grounded caption"])})
        monkeypatch.setattr(daily_card.requests, "get", _raise)
        monkeypatch.setattr(postiz_client.requests, "post", _raise)

        rc = daily_card.run(dry_run=True)
        assert rc == 0

    def test_no_candidates_returns_nonzero_without_staging(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _patch_db(monkeypatch, {"general": _general_row([])})
        monkeypatch.setattr(postiz_client, "stage_candidate", _raise)

        rc = daily_card.run(dry_run=True)
        assert rc == 1
