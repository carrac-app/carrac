"""Sync DB dependency for the FastAPI sub-app.

Wraps ``carrac.db_pool.get_conn`` (a sync context manager that
checks out from the existing psycopg2/SQLite pool, commits on success,
rolls back on exception, and returns the connection to the pool) so it
can be consumed via FastAPI ``Depends()``.

Public, no-auth routes may or may not hit the DB.
We deliberately stay on the sync driver for now — the async DB
swap is a post-cutover follow-up (an explicit non-goal of the
migration). The dependency yields a connection that the existing
sync handlers can use unchanged.

The dependency is intentionally a generator-based ``Depends``: the
request lifecycle drives checkout/release, which is the leak-immune
shape we want as the remaining direct connection call sites are retired.
"""

from __future__ import annotations

from typing import Iterator

from carrac.db_pool import get_conn as _pool_get_conn


def get_db() -> Iterator:
    """Yield a pooled DB connection bound to the current request.

    Usage::

        @router.get("/something")
        def handler(db = Depends(get_db)):
            with db.cursor() as cur:
                cur.execute("SELECT 1")
                ...

    Commit / rollback / putconn are handled by the underlying
    ``carrac.db_pool.get_conn`` context manager.
    """
    with _pool_get_conn() as conn:
        yield conn
