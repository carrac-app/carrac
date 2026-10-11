"""Carrac FastAPI sub-application — strangler-fig migration target.

Mounted at /api/v2 by carrac.asgi. New endpoints land here as Flask
routes are progressively retired.
"""

from carrac.fastapi_app.main import fastapi_app

__all__ = ["fastapi_app"]
