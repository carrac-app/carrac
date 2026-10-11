"""Pydantic request/response models for the FastAPI write surface.

One module per blueprint area, mirroring the
``carrac/fastapi_app/routers/`` layout. Every body model carries
``model_config = ConfigDict(extra="forbid")`` so unknown fields raise
``RequestValidationError`` (400 via the exception handler).
"""

__all__: list[str] = []
