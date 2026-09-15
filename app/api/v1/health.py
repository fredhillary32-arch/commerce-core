from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import InfrastructureError
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness check: is the process itself running? Never touches PostgreSQL."""
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    """Readiness check: can we actually serve requests, PostgreSQL included?"""
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        raise InfrastructureError("Database is not reachable") from exc
    return {"status": "ok", "database": "connected"}
