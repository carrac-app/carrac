"""Error-tracker hygiene — drop scratch-script events, stamp release.

Two independent gaps in error tracking:

1. Ad-hoc `docker exec carrac python -c` / heredoc / /tmp scratch
   investigations are not prod exceptions — importing `carrac` arms
   Sentry as a package side effect, so any typo in a one-off script would
   burn the 3000-event project cap as a false "prod issue".
2. Events without a release tag cannot be tied to
   APP_VERSION, so "is this still happening on the current build?" is
   unanswerable from the issue page.

`should_report_event` is tested directly (the real function, not a mock).
`sentry_sdk.init` is the one place a mock belongs — asserting it is called
with `release=APP_VERSION` is the only way to prove the wiring, not just the
kwargs-builder, without spinning up a real Sentry client.
"""
from unittest.mock import patch

from carrac.observability import should_report_event
from carrac.version import APP_VERSION
from carrac import _sentry_init_kwargs, _maybe_init_sentry


class TestShouldReportEvent:
    """DENY-BY-SHAPE, not an allowlist of names.

    An allowlist is too narrow: the prod crontab launches `python -m
    morning_engine`, `python -m quant.check_alerts`, a dozen
    `python -m quant.collectors.<name>`, and several `scripts/*.py` cron
    jobs — every one a legitimate entrypoint an allowlist would have to
    name individually and would silently suppress until it did. The three
    scratch shapes are the stable, small set:
    `-c` / `-` / `<stdin>`, and anything under /tmp, /dev, or /var/tmp.
    Everything else is kept.
    """

    # --- the three scratch shapes -----------------
    def test_python_dash_c_is_dropped(self):
        assert should_report_event(["-c"]) is False

    def test_stdin_dash_is_dropped(self):
        assert should_report_event(["-"]) is False

    def test_tmp_scratch_script_is_dropped(self):
        assert should_report_event(["/tmp/br.py"]) is False

    # --- other scratch shapes covered by the same deny rule -----------------
    def test_literal_stdin_marker_is_dropped(self):
        assert should_report_event(["<stdin>"]) is False

    def test_nested_tmp_path_is_dropped(self):
        assert should_report_event(["/var/folders/tmp/x/scratch.py"]) is False

    def test_dev_path_is_dropped(self):
        assert should_report_event(["/dev/fd/63"]) is False

    def test_var_tmp_path_is_dropped(self):
        assert should_report_event(["/var/tmp/scratch.py"]) is False

    def test_empty_argv_is_dropped(self):
        assert should_report_event([]) is False

    def test_empty_argv0_is_dropped(self):
        assert should_report_event([""]) is False

    # --- the app server -----------------------------------------------------
    def test_gunicorn_is_kept(self):
        assert should_report_event(["/usr/local/bin/gunicorn"]) is True

    def test_uvicorn_worker_process_is_kept(self):
        assert should_report_event(["/usr/local/bin/uvicorn"]) is True

    def test_worker_py_is_kept(self):
        assert should_report_event(["worker.py"]) is True

    def test_worker_py_with_full_path_is_kept(self):
        assert should_report_event(["/app/worker.py"]) is True

    # --- real shapes read verbatim off prod's crontab (`crontab -l`) -------
    def test_morning_engine_dash_m_invocation_is_kept(self):
        # `python -m morning_engine --mode week_ahead --region europe`
        assert should_report_event(
            ["/app/morning_engine/__main__.py", "--mode", "week_ahead", "--region", "europe"]
        ) is True

    def test_quant_check_alerts_dash_m_invocation_is_kept(self):
        # `python -m quant.check_alerts`
        assert should_report_event(["/app/quant/check_alerts.py"]) is True

    def test_quant_collector_dash_m_invocation_is_kept(self):
        # `python -m quant.collectors.binance_daily`
        assert should_report_event(["/app/quant/collectors/binance_daily.py"]) is True

    def test_alembic_module_invocation_is_kept(self):
        # `python -m alembic ...` — verified empirically against this repo's
        # venv that sys.argv[0] becomes the installed package's
        # .../alembic/__main__.py, not the string "alembic". Kept the same
        # as any other in-tree module under the new deny-by-shape rule.
        assert should_report_event(
            ["/app/.venv/lib/python3.14/site-packages/alembic/__main__.py"]
        ) is True

    def test_scripts_cron_entrypoint_is_kept(self):
        assert should_report_event(["/opt/carrac/scripts/http_error_scan.py"]) is True

    def test_relative_scripts_path_is_kept(self):
        assert should_report_event(["scripts/bugsink-housekeeping.py"]) is True

    def test_ai_cost_alert_cron_is_kept(self):
        assert should_report_event(["/app/scripts/ai_cost_alert.py"]) is True

    def test_paddle_dlq_retry_cron_is_kept(self):
        assert should_report_event(["/app/scripts/paddle_dlq_retry.py"]) is True

    def test_sweep_expired_subs_cron_is_kept(self):
        assert should_report_event(["/app/scripts/sweep_expired_subs.py"]) is True

    def test_idle_in_tx_probe_cron_is_kept(self):
        assert should_report_event(["/app/scripts/probes/idle_in_tx_probe.py"]) is True


class TestReleaseIsStamped:
    def test_init_kwargs_carry_release(self):
        kw = _sentry_init_kwargs("https://k@example.invalid/1")
        assert kw["release"] == APP_VERSION


class TestMaybeInitSentry:
    """sentry_sdk.init is the one place a mock is appropriate — this proves
    the actual wiring, not just that _sentry_init_kwargs looks right."""

    def test_real_entrypoint_arms_sentry_with_release(self):
        with patch("sentry_sdk.init") as mock_init, \
                patch("sentry_sdk.integrations.logging.ignore_logger"):
            armed = _maybe_init_sentry(
                "https://k@example.invalid/1", ["/usr/local/bin/gunicorn"]
            )
        assert armed is True
        mock_init.assert_called_once()
        _, kwargs = mock_init.call_args
        assert kwargs["release"] == APP_VERSION
        assert kwargs["dsn"] == "https://k@example.invalid/1"

    def test_scratch_script_does_not_arm_sentry(self):
        with patch("sentry_sdk.init") as mock_init:
            armed = _maybe_init_sentry("https://k@example.invalid/1", ["-c"])
        assert armed is False
        mock_init.assert_not_called()

    def test_no_dsn_does_not_arm_sentry(self):
        with patch("sentry_sdk.init") as mock_init:
            armed = _maybe_init_sentry("", ["/usr/local/bin/gunicorn"])
        assert armed is False
        mock_init.assert_not_called()
