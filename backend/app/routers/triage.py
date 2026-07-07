
from fastapi import APIRouter, Depends
from typing import List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel

from app.routers.auth import get_current_user
from app.storage import _users, latest_vitals_by_user, compute_score_from_latest

router = APIRouter()

class TriageItem(BaseModel):
    user_id: int
    username: str
    last_vital_at: Optional[datetime]
    last_score: int
    level: str
    wait_minutes: float
    priority: float

@router.get("/", response_model=List[TriageItem])
async def get_triage(current_user=Depends(get_current_user)):
    # naive-UTC 'now' for simple diff against stored naive timestamps
    now = datetime.now(timezone.utc).astimezone().replace(tzinfo=None)
    items: List[TriageItem] = []

    for uid, usr in _users.items():
        latest = latest_vitals_by_user(uid)
        if latest:
            last_time = max(v.timestamp for v in latest.values())
            wait_minutes = max(0.0, (now - last_time).total_seconds() / 60.0)
            score, level = compute_score_from_latest(latest)
        else:
            last_time = None
            wait_minutes = 9999.0   # pushes users with no vitals down the list
            score, level = 0, "normal"

        # Priority rule: emphasize condition (score) but consider wait time
        priority = 2 * score + 0.05 * wait_minutes

        items.append(TriageItem(
            user_id=uid,
            username=usr.username,
            last_vital_at=last_time,
            last_score=score,
            level=level,
            wait_minutes=wait_minutes,
            priority=priority
        ))

    # Highest priority first
    items.sort(key=lambda x: x.priority, reverse=True)
    return items
