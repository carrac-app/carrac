"""Carrac Python SDK — trading market data, plays, alerts, usage."""

from .client import Client
from .errors import (
    CarracError,
    AuthError,
    RateLimitError,
    NotFoundError,
    ApiError,
)

__version__ = "0.2.0"

__all__ = [
    "Client",
    "CarracError",
    "AuthError",
    "RateLimitError",
    "NotFoundError",
    "ApiError",
]
