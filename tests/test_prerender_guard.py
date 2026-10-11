"""The morning-report chart pre-render must never storm the gunicorn pool.

Failure mode: a parallel `_prerender_morning_charts()` fires ~86 loopback
`/api/screenshot/<sym>/<tf>` requests into the same 4-worker gunicorn pool.
Each screenshot does goto(30s implicit) + wait_for_function(30s)
~= 60s server-side == gunicorn --timeout 60 -> SIGTERM, on every
regional report save.

These tests pin three invariants:
  1. the pre-render is serialized to a single worker
  2. the Playwright timeout is well under gunicorn's 60s worker timeout
  3. a cross-worker Redis NX lock prevents overlapping dispatches from stacking
"""
from unittest import mock

from carrac.reports import routes as report_routes
from carrac.reports import screenshots


class _SyncThread:
    """Stand-in for threading.Thread that runs target() synchronously on start()."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def test_prerender_serialized_to_one_worker():
    assert report_routes._PRERENDER_MAX_WORKERS == 1


def test_screenshot_timeout_safely_under_gunicorn_timeout():
    # gunicorn runs --timeout 60; a single screenshot does goto (nav) then
    # wait_for_function (render). Their sum + browser/teardown overhead must
    # stay clear of 60s so a stalled render fails fast instead of being
    # SIGTERM-killed. ~40s leaves comfortable margin.
    nav = screenshots.SCREENSHOT_NAV_TIMEOUT_MS
    render = screenshots.SCREENSHOT_RENDER_TIMEOUT_MS
    assert (nav + render) / 1000.0 < 55


def _run_prerender(app, lock_acquired):
    fake_redis = mock.Mock()
    fake_redis.set.return_value = lock_acquired
    with app.app_context(), \
         mock.patch("carrac.reports.routes.threading.Thread", _SyncThread), \
         mock.patch("carrac.redis_cache.get_redis", return_value=fake_redis), \
         mock.patch("carrac.reports.routes.ThreadPoolExecutor") as pool_cls:
        report_routes._prerender_morning_charts()
    return fake_redis, pool_cls


def test_overlap_lock_skips_when_already_running(app):
    fake_redis, pool_cls = _run_prerender(app, lock_acquired=False)
    fake_redis.set.assert_called_once()  # attempted the NX lock
    pool_cls.assert_not_called()          # ...and bailed without spawning the pool


def test_prerender_runs_and_releases_lock_when_free(app):
    fake_redis, pool_cls = _run_prerender(app, lock_acquired=True)
    pool_cls.assert_called_once()  # lock free -> pool spawned
    fake_redis.delete.assert_called_once_with("mcd:prerender:running")
