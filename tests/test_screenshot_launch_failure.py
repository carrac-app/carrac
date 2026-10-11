"""A browser that fails to launch must not leave Playwright's event loop
running on the calling thread.

Playwright's sync API installs a running asyncio loop on the thread that
starts it. The screenshot renderer runs on long-lived worker threads (the
Flask executor, asyncio's default pool); a loop left running there makes
every later request on that thread fail inside asgiref.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_failed_browser_launch_leaves_no_running_loop(monkeypatch, tmp_path):
    pytest.importorskip("playwright.sync_api")
    # Point Playwright at an empty browser directory so the launch fails
    # whether or not browsers are installed on this machine.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))
    from carrac.reports import screenshots

    def launch_then_inspect():
        with pytest.raises(Exception):
            screenshots._get_persistent_browser()
        return asyncio._get_running_loop()

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(launch_then_inspect).result() is None
