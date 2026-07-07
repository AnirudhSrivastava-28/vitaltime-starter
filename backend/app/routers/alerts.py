
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from datetime import datetime
from pydantic import BaseModel

from app.routers.auth import get_current_user
from app.storage import add_alert, _alerts

router = APIRouter()

class Alert(BaseModel):
    id: int
    user_id: int
    message: str
    timestamp: datetime
    acknowledged: bool = False

class CreateAlert(BaseModel):
    user_id: int
    message: str

@router.get("/", response_model=List[Alert])
async def list_alerts(current_user=Depends(get_current_user)):
    return [Alert(**a.__dict__) for a in _alerts.values()]

@router.post("/", response_model=Alert, status_code=status.HTTP_201_CREATED)
async def create_alert(alert: CreateAlert, current_user=Depends(get_current_user)):
    a = add_alert(alert.user_id, alert.message)
    return Alert(**a.__dict__)
