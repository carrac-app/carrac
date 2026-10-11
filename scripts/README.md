# scripts/

Helpers for developing and running a self-hosted Carrac. Run them from the
repository root. The scheduled ones (collectors, reports, alerts) are wired up
in [`deploy/crontab.example`](../deploy/crontab.example).

## Developer scripts

| Script | What it does |
|---|---|
| `quickstart.sh` | Generates `.env` with real secrets, starts the dev stack, and runs the six-step smoke (`make dev`). |
| `configure.sh` | Walks through the optional keys in `.env` (data, AI, email, notifications); `--list` shows what is set (`make configure`). |
| `browser-smoke.py` | Drives real Chromium through the app and fails on any JavaScript error (needs `SMOKE_LOGIN_EMAIL` / `SMOKE_LOGIN_PASSWORD`). |
| `alembic_stamp_if_fresh.py` | The migration runner the compose `migrate` service uses: migrates a fresh database, refuses an untracked existing one unless `ALEMBIC_ADOPT=1`. |
| `init_postgres.pgsql` | Baseline schema executed by the first alembic revision. |
| `check_schema_drift.py` | Compares two Postgres schemas column by column; CI uses it to check a fresh bootstrap against the reference schema. |
| `check_write_columns.py` | Static sweep: every column the code INSERTs or UPDATEs must exist in the reference schema. |
| `check-split-layout-equivalence.sh` | Minifies a `static/js` file and its copy at a git ref, to show an edit changed formatting only. |
| `check_provenance.py` | Traces the numbers in a stored report back to the briefing that produced them. |
| `validate_liq_heatmap.py` | Replays stored liquidation events against the modelled heatmap and reports the hit rate versus a random baseline. |
| `backfill_correlations_fdr_ci.py` | Fills `q_value` and confidence intervals on older quant correlation rows. |
| `backfill_spot_journal.py` | One-time sweep promoting older spot-buy drawings into journal trades. |
| `purge_synthetic_bars.py` | Removes persisted synthetic bars from the daily caches (dry run by default; `--execute` to apply). |
| `migrate_to_postgres.py` | One-off copy of the legacy SQLite databases into PostgreSQL. |
| `migrate_scenarios_v1_to_v2.py` | One-off conversion of drawing-based scenarios to the structured scenario tables. |

## Maintenance and scheduled scripts

Run these inside the app container (`docker compose exec carrac python scripts/<name>`),
or from cron as shown in `deploy/crontab.example`.

| Script | What it does |
|---|---|
| `cache_integrity_check.py` | Monitor for silent corruption of the `data/*.csv` OHLCV caches. Stdlib only, so it also runs on the host; paths come from `CARRAC_DATA_DIR`, `CARRAC_CACHE_BASELINE` and `CARRAC_CACHE_STATE`, and alerts go to `NTFY_BASE` / `NTFY_CARRAC_TOPIC` when set. |
| `ai_cost_alert.py` | Nightly alert when the day's AI spend crosses the warning and alert thresholds. |
| `ai_log_retention_prune.py` | Daily prune of AI prompt and response logs past the retention window. |
| `paddle_dlq_retry.py` | Retries failed payment webhooks and alerts on events that exhausted their retries. |
| `sweep_expired_subs.py` | Hourly downgrade of users whose subscription has expired. |
| `backfill_daily_history.py` | Backfills deeper daily history for the session-traded markets from a copy of the daily caches. |
| `purge_weekend_bars.py` | Removes phantom weekend-dated rows from weekday-instrument daily caches. |
| `quarantine_wrong_instrument_caches.py` | Moves daily caches that hold the wrong instrument out of service. |
