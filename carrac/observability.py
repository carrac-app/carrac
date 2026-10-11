"""Sentry process-hygiene helpers.

Importing `carrac` initialises Sentry as a package side effect
(see the module-load bootstrap in `carrac/__init__.py`), so any ad-hoc
`docker exec carrac python -c '...'` / heredoc / `/tmp` scratch script used
to report a typo or a missing service as a "prod issue" too. Such scratch
runs showed up as a large share of backend "exceptions" in error tracking,
with `sys.argv` of `["-c"]`, `["-"]`, or `["/tmp/br.py"]`.

`should_report_event()` is a pure DENY-BY-SHAPE rule, not an allowlist of
names. An allowlist is too narrow: prod's
crontab alone launches `python -m morning_engine --mode …`
(argv0=`/app/morning_engine/__main__.py`), `python -m quant.check_alerts`,
a dozen `python -m quant.collectors.<name>`, and several `scripts/*.py`
cron jobs — every real module/package in the app tree is a legitimate
entrypoint, and a name-based allowlist would need updating every time a new
one is added, silently suppressing it until then. The three scratch shapes
seen in practice are a much smaller, more stable set to describe:

  - `-c` / `-` / `<stdin>`        — python -c '...', heredocs, `python -`
  - anything under /tmp, /dev,
    or /var/tmp                   — one-off scratch files

Everything else — gunicorn, uvicorn, worker.py, any `-m pkg.mod` invocation,
any `scripts/*.py` cron entrypoint — is a real path inside the application
tree and is kept.
"""

_SCRATCH_ARGV0 = frozenset({"-c", "-", "<stdin>"})
_DENIED_PATH_MARKERS = ("/tmp/", "/dev/", "/var/tmp/")


def should_report_event(argv):
    """True iff `argv` (typically `sys.argv`) is a real process, not a
    scratch investigation.

    Pure function — no I/O, no globals besides the two denylists above — so
    it can be unit-tested directly against the exact argv shapes seen in
    production (prod crontab, docker-compose commands) and in error tracking.
    """
    if not argv:
        return False
    argv0 = argv[0]
    if not argv0:
        return False
    if argv0 in _SCRATCH_ARGV0:
        return False
    normalized = str(argv0).replace("\\", "/")
    if any(marker in normalized for marker in _DENIED_PATH_MARKERS):
        return False
    return True
