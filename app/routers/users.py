from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.schemas import UserCreate, UserOut
from app.security import current_user, hash_password

router = APIRouter(tags=["users"])


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    user = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        risk_score=payload.risk_score,
        balance=0,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # 409, not 400: the request was well-formed, it conflicts with state.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from None
    db.refresh(user)
    return user


@router.get("/users/{user_id}", response_model=UserOut)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    caller: User = Depends(current_user),
) -> User:
    if user_id != caller.id:
        # 404 rather than 403: telling a caller that a user id exists but is
        # not theirs leaks the id space.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return caller
