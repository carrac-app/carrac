"""Public no-auth endpoints. Only the strangler-mount probe lives here."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/_ping")
def ping():
    return {"ok": True}
