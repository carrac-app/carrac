"""Carrac API client.

Thin wrapper around httpx. Resource namespaces (charts, plays, alerts, ...)
organize the surface; all I/O goes through Client._request so retries and
error handling stay in one place.
"""

import os
import time

import httpx

from .errors import AuthError, NotFoundError, RateLimitError, ApiError

DEFAULT_TIMEOUT = 30.0
DEFAULT_USER_AGENT = "carrac-python-sdk/0.2.0"


class Client:
    """HTTP client for the Carrac API.

    Args:
        api_key: your Carrac API key. Falls back to CARRAC_API_KEY env var.
            Also accepts the legacy CONVEXITY_API_KEY for backward compatibility.
        base_url: the Carrac server to talk to (your own instance). Falls
            back to CARRAC_BASE_URL. Required: there is no default host.
        timeout: request timeout in seconds.
        max_retries: how many times to automatically retry on 429 / 5xx.
    """

    def __init__(self, api_key=None, base_url=None, timeout=DEFAULT_TIMEOUT,
                 max_retries=3, user_agent=DEFAULT_USER_AGENT):
        self.api_key = (
            api_key
            or os.environ.get("CARRAC_API_KEY")
            or os.environ.get("CONVEXITY_API_KEY")  # backward-compat
        )
        if not self.api_key:
            raise ValueError(
                "No API key provided. Pass api_key=... or set CARRAC_API_KEY."
            )
        resolved = (
            base_url
            or os.environ.get("CARRAC_BASE_URL")
            or os.environ.get("CONVEXITY_BASE_URL")  # backward-compat
        )
        if not resolved:
            raise ValueError(
                "No base URL provided. Pass base_url=... (the address of your "
                "own Carrac instance, e.g. http://localhost:5002) or set "
                "CARRAC_BASE_URL."
            )
        self.base_url = resolved.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "X-API-Key": self.api_key,
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
        )

        # Resource namespaces
        from .resources import Charts, Plays, Alerts, Composite, Scanner, Usage, Keys
        self.charts = Charts(self)
        self.plays = Plays(self)
        self.alerts = Alerts(self)
        self.composite = Composite(self)
        self.scanner = Scanner(self)
        self.usage = Usage(self)
        self.keys = Keys(self)

    # ── Context manager support ──
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        self._http.close()

    # ── Core request ──

    def _request(self, method, path, *, params=None, json=None):
        attempts = 0
        while True:
            attempts += 1
            try:
                resp = self._http.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:
                if attempts > self.max_retries:
                    raise ApiError(f"Network error: {exc}") from exc
                time.sleep(min(2 ** (attempts - 1), 10))
                continue

            if resp.status_code == 429 and attempts <= self.max_retries:
                retry_after = int(resp.headers.get("Retry-After", "1"))
                time.sleep(min(retry_after, 30))
                continue

            if 500 <= resp.status_code < 600 and attempts <= self.max_retries:
                time.sleep(min(2 ** (attempts - 1), 10))
                continue

            self._raise_for_status(resp)

            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise ApiError(f"Non-JSON response: {resp.text[:200]}") from exc

    def _raise_for_status(self, resp):
        if resp.is_success:
            return
        body = {}
        try:
            body = resp.json()
        except ValueError:
            pass
        message = body.get("error") or resp.text or resp.reason_phrase

        if resp.status_code in (401, 403):
            raise AuthError(message, status_code=resp.status_code, response=body)
        if resp.status_code == 404:
            raise NotFoundError(message, status_code=resp.status_code, response=body)
        if resp.status_code == 429:
            raise RateLimitError(
                message,
                retry_after=int(resp.headers.get("Retry-After", "0")),
                status_code=429,
                response=body,
            )
        raise ApiError(message, status_code=resp.status_code, response=body)

    # ── Shortcut methods ──
    def get(self, path, **params):
        return self._request("GET", path, params=params or None)

    def post(self, path, json=None, **params):
        return self._request("POST", path, params=params or None, json=json)

    def put(self, path, json=None, **params):
        return self._request("PUT", path, params=params or None, json=json)

    def delete(self, path, **params):
        return self._request("DELETE", path, params=params or None)
