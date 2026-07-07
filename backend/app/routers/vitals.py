
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from datetime import datetime
from pydantic import BaseModel

from app.routers.auth import get_current_user
from app.storage import add_vital, _vitals, StoredVital, maybe_alert_from_vital

router = APIRouter()

class Vital(BaseModel):
    id: int
    user_id: int
    type: str
    value: float
    timestamp: datetime

class CreateVital(BaseModel):
    user_id: int
    type: str
    value: float

@router.get("/", response_model=List[Vital])
async def list_vitals(current_user=Depends(get_current_user)):
    return [Vital(**v.__dict__) for v in _vitals.values()]

@router.get("/{vital_id}", response_model=Vital)
async def get_vital(vital_id: int, current_user=Depends(get_current_user)):
    v = _vitals.get(vital_id)
    if not v:
        raise HTTPException(status_code=404, detail="Vital not found")
    return Vital(**v.__dict__)

@router.post("/", response_model=Vital, status_code=status.HTTP_201_CREATED)
async def create_vital(vital: CreateVital, current_user=Depends(get_current_user)):
    v = add_vital(vital.user_id, vital.type, vital.value)
    maybe_alert_from_vital(v)
    return Vital(**v.__dict__)
