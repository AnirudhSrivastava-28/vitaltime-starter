
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional
from pydantic import BaseModel, EmailStr

from app.routers.auth import get_current_user
from app.storage import add_user, update_user, delete_user, _users

router = APIRouter()

class User(BaseModel):
    id: int
    username: str
    full_name: str
    email: EmailStr
    disabled: bool = False

class CreateUser(BaseModel):
    username: str
    full_name: str
    email: EmailStr
    password: str

class UpdateUser(BaseModel):
    username: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    disabled: Optional[bool] = None

@router.get("/", response_model=List[User])
async def list_users(current_user=Depends(get_current_user)):
    return [User(**{k:v for k,v in u.__dict__.items() if k != "hashed_password"}) for u in _users.values()]

@router.get("/{user_id}", response_model=User)
async def get_user(user_id: int, current_user=Depends(get_current_user)):
    u = _users.get(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return User(**{k:v for k,v in u.__dict__.items() if k != "hashed_password"})

@router.post("/", response_model=User, status_code=status.HTTP_201_CREATED)
async def create_user(user: CreateUser, current_user=Depends(get_current_user)):
    try:
        u = add_user(user.username, user.full_name, user.email, user.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return User(**{k:v for k,v in u.__dict__.items() if k != "hashed_password"})

@router.put("/{user_id}", response_model=User)
async def update_user_route(user_id: int, payload: UpdateUser, current_user=Depends(get_current_user)):
    try:
        u = update_user(user_id,
                        username=payload.username,
                        full_name=payload.full_name,
                        email=payload.email,
                        password=payload.password,
                        disabled=payload.disabled)
    except KeyError:
        raise HTTPException(status_code=404, detail="User not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return User(**{k:v for k,v in u.__dict__.items() if k != "hashed_password"})

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_route(user_id: int, current_user=Depends(get_current_user)):
    if user_id not in _users:
        raise HTTPException(status_code=404, detail="User not found")
    delete_user(user_id)
    return None
