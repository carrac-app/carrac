"""slowapi Limiter for the FastAPI sub-app.

This wires per-route rate limiting into the strangler-fig FastAPI side.
The limiter shares the same Redis backend as ``flask-limiter`` (REDIS_URL,
db 0) so a single shared user hitting both runtimes during the cutover
period sees one combined budget — not two separate ones that double the
real ceiling.

Key-func reads ``request.state.rate.key`` populated by the
``RateContext`` chain. If the request never went through
``classify_request`` (e.g. middleware ordering bug, or the limiter ran
before the classify ``Depends``), we fall back to the remote address so
the limiter still gates abusive traffic.

Storage falls back to ``memory://`` when ``REDIS_URL`` is unset (CI/tests).
The production deploy guarantees Redis on db 0 — this is the same URL
flask-limiter uses today.

The Redis storage hit runs off the event loop for async routes.
See ``AsyncOffloadLimiter`` below for why and how.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
from typing import Any, Callable, List, Optional, Union

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

logger = logging.getLogger("carrac.fastapi.limiter")


def _rate_key(request: Request) -> str:
    """Read the ``RateContext`` bucket key, fall back to remote IP."""
    ctx = getattr(request.state, "rate", None)
    if ctx is not None and getattr(ctx, "key", ""):
        return ctx.key
    # Defensive: classify_request should always have populated state.rate
    # by the time the limiter fires. If it hasn't, something is wrong with
    # middleware ordering — log so the bug doesn't hide.
    logger.debug(
        "slowapi key_func fallback to remote address (request.state.rate missing)"
    )
    return get_remote_address(request)


class AsyncOffloadLimiter(Limiter):
    """A ``slowapi.Limiter`` whose ``@limit(...)`` check runs off the event loop.

    slowapi 0.1.9's own decorator (``slowapi/extension.py``,
    ``Limiter._Limiter__limit_decorator``) calls ``self._check_request_limit(...)``
    *synchronously and un-awaited* inside the ``async_wrapper`` it builds for
    coroutine routes. That call walks into the ``limits`` library's Redis
    storage (``limits/storage/redis.py::RedisStorage``), which is built on the
    plain sync ``redis`` client (``self.dependency.from_url(uri, ...)`` —
    verified no ``redis.asyncio`` anywhere in that module) — a genuine
    blocking socket round-trip executed inline on the asyncio event loop for
    every ``async def`` route decorated with ``@limiter.limit(...)``. Under
    Redis contention (e.g. a concurrent multi-MB blob GET), that single
    blocking call stalls every coroutine on the worker, including unrelated
    interactive traffic (e.g. ``/api/chart-internal``), a chart-latency root
    cause.

    Verified there is no async storage/check path to opt into: in the
    installed slowapi 0.1.9, ``_check_request_limit``, ``__evaluate_limits``,
    and ``self.limiter.hit(...)`` are all plain ``def``s — nothing to
    ``await``. And we can't patch the vendored ``async_wrapper`` itself (not
    our file to touch). So rather than reimplement slowapi's registration
    internals, this override pre-empts the blocking call: it builds slowapi's
    REAL decorator via ``super().limit(...)`` first (so limit registration —
    ``self._route_limits``/``self._dynamic_route_limits`` — and header
    injection stay byte-for-byte identical to stock slowapi, which matters
    because ``SlowAPIMiddleware``'s ``_should_exempt`` checks
    ``handler.__name__ in limiter._route_limits`` to skip its own redundant
    check for decorated routes), then wraps the result in an outer async
    layer that performs the SAME check via ``asyncio.to_thread`` — the offload
    idiom already used everywhere else in ``fastapi_app/`` (composite.py,
    journal.py, curl_charts.py, ...) — and sets
    ``request.state._rate_limiting_complete`` first. slowapi's own inner
    check sees the flag already set and skips its (redundant, blocking) call,
    so the storage hit happens exactly once, off the loop, for every
    ``async def`` route.

    ``def`` (sync) routes are left untouched — FastAPI/Starlette already runs
    those via a thread pool (``run_in_threadpool``), so slowapi's inline
    check there was never a loop-blocking risk. (``curl_charts.curl_overview``
    is the one sync route in this codebase carrying ``@limiter.limit(...)``.)

    NOTE: this override only covers ``.limit(...)`` — the only decorator this
    codebase uses today (verified via ``rg '@limiter\\.' carrac/fastapi_app/``:
    every hit is ``.limit(``, none are ``.shared_limit(`` or ``.exempt(``). If
    a future route reaches for ``.shared_limit(...)`` instead, it will run
    slowapi's original (blocking-on-async-routes) path unless offloaded too.
    """

    def limit(
        self,
        limit_value: Any,
        key_func: Optional[Callable[..., str]] = None,
        per_method: bool = False,
        methods: Optional[List[str]] = None,
        error_message: Any = None,
        exempt_when: Optional[Callable[..., bool]] = None,
        cost: Union[int, Callable[..., int]] = 1,
        override_defaults: bool = True,
    ) -> Callable:
        # Build slowapi's real decorator unchanged — this is what performs
        # registration into self._route_limits / self._dynamic_route_limits.
        inner_decorator = super().limit(
            limit_value,
            key_func=key_func,
            per_method=per_method,
            methods=methods,
            error_message=error_message,
            exempt_when=exempt_when,
            cost=cost,
            override_defaults=override_defaults,
        )

        def decorator(func: Callable) -> Callable:
            slowapi_wrapped = inner_decorator(func)
            if not inspect.iscoroutinefunction(func):
                return slowapi_wrapped

            # Mirror slowapi's own request-argument lookup (extension.py
            # __limit_decorator) so we read the exact same object it does.
            request_idx: Optional[int] = None
            for i, param in enumerate(inspect.signature(func).parameters.values()):
                if param.name in ("request", "websocket"):
                    request_idx = i
                    break

            @functools.wraps(func)
            async def offloaded_wrapper(*args: Any, **kwargs: Any) -> Any:
                if self.enabled and request_idx is not None:
                    request = kwargs.get(
                        "request", args[request_idx] if args else None
                    )
                    # Only short-circuit slowapi's own check when we're sure
                    # we're looking at the real Request it would use too — if
                    # this ever doesn't hold, fall through unchanged below and
                    # let slowapi's original (still correct, just inline)
                    # check run instead of raising or silently skipping it.
                    if (
                        isinstance(request, Request)
                        and self._auto_check
                        and not getattr(
                            request.state, "_rate_limiting_complete", False
                        )
                    ):
                        await asyncio.to_thread(
                            self._check_request_limit, request, func, False
                        )
                        request.state._rate_limiting_complete = True
                return await slowapi_wrapped(*args, **kwargs)

            return offloaded_wrapper

        return decorator


limiter = AsyncOffloadLimiter(
    key_func=_rate_key,
    storage_uri=os.environ.get("REDIS_URL", "memory://"),
    headers_enabled=True,
)


__all__ = ["limiter", "AsyncOffloadLimiter"]
