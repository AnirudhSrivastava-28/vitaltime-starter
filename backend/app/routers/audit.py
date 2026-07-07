
from fastapi import APIRouter, Depends, Query
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel

from app.routers.auth import get_current_user
from app.storage import list_audit

router = APIRouter()

class AuditEntry(BaseModel):
    id: int
    ts: datetime
    event: str
    user_id: Optional[int]
    details: dict

@router.get("/", response_model=List[AuditEntry])
async def get_audit(
    limit: int = Query(100, ge=1, le=1000),
    user_id: Optional[int] = None,
    current_user=Depends(get_current_user)
):
    rows = list_audit(limit=limit, user_id=user_id)
    return [AuditEntry(id=r.id, ts=r.ts, event=r.event, user_id=r.user_id, details=r.details) for r in rows]
