# backend/app/storage.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, List, Tuple
from passlib.context import CryptContext

# Password hashing context for demo users
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ─────────────────────────────────────────────────────────────────────────────
# USERS (in-memory)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StoredUser:
    id: int
    username: str
    full_name: str
    email: str
    hashed_password: str
    disabled: bool = False

_users: Dict[int, StoredUser] = {}
_users_by_username: Dict[str, int] = {}
_user_autoinc = 0

def _seed() -> None:
    """Seed a demo user: alice / secret"""
    global _user_autoinc
    if not _users:
        _user_autoinc = 1
        u = StoredUser(
            id=1,
            username="alice",
            full_name="Alice Example",
            email="alice@example.com",
            hashed_password=pwd_context.hash("secret"),
            disabled=False,
        )
        _users[u.id] = u
        _users_by_username[u.username] = u.id

_seed()

def get_user_by_username(username: str) -> Optional[StoredUser]:
    uid = _users_by_username.get(username)
    return _users.get(uid) if uid else None

def add_user(username: str, full_name: str, email: str, password: str) -> StoredUser:
    global _user_autoinc
    if username in _users_by_username:
        raise ValueError("username already exists")
    _user_autoinc += 1
    u = StoredUser(
        id=_user_autoinc,
        username=username,
        full_name=full_name,
        email=email,
        hashed_password=pwd_context.hash(password),
        disabled=False,
    )
    _users[u.id] = u
    _users_by_username[username] = u.id
    return u

def update_user(
    user_id: int,
    *,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    email: Optional[str] = None,
    password: Optional[str] = None,
    disabled: Optional[bool] = None,
) -> StoredUser:
    u = _users.get(user_id)
    if not u:
        raise KeyError("user not found")
    if username and username != u.username:
        if username in _users_by_username:
            raise ValueError("username already exists")
        del _users_by_username[u.username]
        _users_by_username[username] = u.id
        u.username = username
    if full_name is not None:
        u.full_name = full_name
    if email is not None:
        u.email = email
    if password is not None:
        u.hashed_password = pwd_context.hash(password)
    if disabled is not None:
        u.disabled = disabled
    return u

def delete_user(user_id: int) -> None:
    u = _users.pop(user_id, None)
    if u:
        _users_by_username.pop(u.username, None)

# ─────────────────────────────────────────────────────────────────────────────
# VITALS (in-memory)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StoredVital:
    id: int
    user_id: int
    type: str  # "hr", "sbp", "dbp", "spo2", "temp_c", "fall"
    value: float
    timestamp: datetime

_vitals: Dict[int, StoredVital] = {}
_vital_autoinc = 0

def add_vital(user_id: int, type: str, value: float) -> StoredVital:
    """Ingest a vital reading and append to audit."""
    global _vital_autoinc
    _vital_autoinc += 1
    v = StoredVital(
        id=_vital_autoinc,
        user_id=user_id,
        type=type,
        value=value,
        timestamp=datetime.utcnow(),
    )
    _vitals[v.id] = v
    add_audit(
        "vital_ingested",
        user_id=user_id,
        details={"vital_id": v.id, "type": type, "value": value, "ts": v.timestamp.isoformat()},
    )
    return v

def latest_vitals_by_user(user_id: int) -> Dict[str, StoredVital]:
    """Return the latest vital per type for a user."""
    latest: Dict[str, StoredVital] = {}
    for v in _vitals.values():
        if v.user_id != user_id:
            continue
        prev = latest.get(v.type)
        if (prev is None) or (v.timestamp > prev.timestamp):
            latest[v.type] = v
    return latest

# ─────────────────────────────────────────────────────────────────────────────
# ALERTS (in-memory)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StoredAlert:
    id: int
    user_id: int
    message: str
    timestamp: datetime
    acknowledged: bool = False

_alerts: Dict[int, StoredAlert] = {}
_alert_autoinc = 0

def add_alert(user_id: int, message: str) -> StoredAlert:
    global _alert_autoinc
    _alert_autoinc += 1
    a = StoredAlert(
        id=_alert_autoinc,
        user_id=user_id,
        message=message,
        timestamp=datetime.utcnow(),
    )
    _alerts[a.id] = a
    add_audit(
        "alert_created",
        user_id=user_id,
        details={"alert_id": a.id, "message": message, "ts": a.timestamp.isoformat()},
    )
    return a

# Basic rule set (demo only; not medical advice)
ALERT_RULES = {
    "hr": lambda v: v < 50 or v > 140,
    "spo2": lambda v: v < 90,
    "temp_c": lambda v: v < 35.0 or v > 39.0,
    "sbp": lambda v: v < 90 or v > 180,
    "fall": lambda v: v >= 1.0,
}

def maybe_alert_from_vital(v: StoredVital) -> StoredAlert | None:
    rule = ALERT_RULES.get(v.type)
    if rule and rule(float(v.value)):
        msg = f"Abnormal {v.type} = {v.value} at {v.timestamp.isoformat()}"
        return add_alert(v.user_id, msg)
    return None

# ─────────────────────────────────────────────────────────────────────────────
# AUDIT LOG (in-memory)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StoredAudit:
    id: int
    ts: datetime
    event: str
    user_id: Optional[int]
    details: dict

_audit: Dict[int, StoredAudit] = {}
_audit_autoinc = 0

def add_audit(event: str, *, user_id: Optional[int] = None, details: dict | None = None) -> StoredAudit:
    global _audit_autoinc
    _audit_autoinc += 1
    entry = StoredAudit(
        id=_audit_autoinc,
        ts=datetime.utcnow(),
        event=event,
        user_id=user_id,
        details=details or {},
    )
    _audit[entry.id] = entry
    return entry

def list_audit(limit: int = 100, user_id: Optional[int] = None) -> List[StoredAudit]:
    rows = sorted(_audit.values(), key=lambda e: e.id, reverse=True)
    if user_id is not None:
        rows = [r for r in rows if r.user_id == user_id]
    return rows[:limit]

# ─────────────────────────────────────────────────────────────────────────────
# TRIAGE scoring helper
# ─────────────────────────────────────────────────────────────────────────────

def compute_score_from_latest(latest: Dict[str, StoredVital]) -> Tuple[int, str]:
    """Compute a simple early-warning-like score and level from latest vitals."""
    hr = float(latest.get("hr").value) if latest.get("hr") else None
    sbp = float(latest.get("sbp").value) if latest.get("sbp") else None
    spo2 = float(latest.get("spo2").value) if latest.get("spo2") else None
    temp_c = float(latest.get("temp_c").value) if latest.get("temp_c") else None
    fall = float(latest.get("fall").value) if latest.get("fall") else 0.0

    score = 0
    # HR
    if hr is not None:
        if hr < 50:
            score += 1
        elif 101 <= hr <= 120:
            score += 1
        elif 121 <= hr <= 140:
            score += 2
        elif hr > 140:
            score += 3
    # SBP
    if sbp is not None:
        if sbp < 90:
            score += 3
        elif 90 <= sbp <= 100:
            score += 2
        elif 101 <= sbp <= 110:
            score += 1
        elif sbp > 180:
            score += 2
    # SpO2
    if spo2 is not None:
        if spo2 < 90:
            score += 3
        elif 90 <= spo2 <= 92:
            score += 2
        elif 93 <= spo2 <= 94:
            score += 1
    # Temp
    if temp_c is not None:
        if temp_c < 35.0:
            score += 2
        elif 38.0 <= temp_c <= 39.0:
            score += 1
        elif temp_c > 39.0:
            score += 2
    # Fall
    if fall and fall >= 1.0:
        score += 3

    level = "normal"
    if score >= 7:
        level = "critical"
    elif score >= 3:
        level = "elevated"
    return score, level
