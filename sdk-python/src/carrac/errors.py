"""SDK error hierarchy.

All API failures raise a subclass of CarracError. Callers can catch the
base class to handle any API failure, or a specific subclass for targeted
recovery (e.g. retrying on RateLimitError).
"""


class CarracError(Exception):
    """Base class for all SDK errors."""

    def __init__(self, message, status_code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class AuthError(CarracError):
    """401/403 — missing, invalid, or insufficient API key / tier."""


class NotFoundError(CarracError):
    """404 — resource does not exist or is not visible to this key."""


class RateLimitError(CarracError):
    """429 — sliding-window budget exceeded.

    `retry_after` is seconds until the soonest window frees up.
    """

    def __init__(self, message, retry_after=0, status_code=429, response=None):
        super().__init__(message, status_code=status_code, response=response)
        self.retry_after = retry_after


class ApiError(CarracError):
    """5xx or unclassified 4xx — server-side failure, treat as transient."""
