from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import AuthToken, User
from app.schemas import LoginRequest, TokenOut
from app.security import (
    bearer_scheme,
    current_user,
    hash_password,
    issue_token,
    token_digest,
    verify_password,
)

router = APIRouter(tags=["auth"])

# Verifying against a throwaway hash keeps the response time for an unknown
# email close to that of a known one, so the endpoint does not answer "does
# this address have an account?" through a timing side channel.
_DUMMY_HASH = hash_password("timing-equalisation-placeholder")

# Failed-login attempts, keyed by email.
#
# This is in-process state: it protects a single worker, so with N workers an
# attacker gets N times the allowance, and a restart clears it. That is a real
# limitation, not a hidden one - a deployment behind more than one worker
# should enforce this at the gateway or in Redis instead. It is here because
# an unthrottled login endpoint is worse than an imperfectly throttled one.
_failed_attempts: dict[str, deque[float]] = defaultdict(deque)


def _record_failure(email: str) -> None:
    _failed_attempts[email].append(time.monotonic())


def _is_locked_out(email: str) -> bool:
    window = settings.login_window_seconds
    cutoff = time.monotonic() - window
    attempts = _failed_attempts[email]
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return len(attempts) >= settings.login_max_attempts


def _clear_failures(email: str) -> None:
    _failed_attempts.pop(email, None)


def reset_login_throttle() -> None:
    """Test hook - clears throttle state between cases."""
    _failed_attempts.clear()


@router.post("/auth/login", response_model=TokenOut)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenOut:
    email = payload.email.lower()

    if _is_locked_out(email):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(settings.login_window_seconds)},
        )

    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()

    if user is None:
        verify_password(payload.password, _DUMMY_HASH)
        _record_failure(email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    if not verify_password(payload.password, user.password_hash):
        _record_failure(email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    _clear_failures(email)

    # Opportunistically drop this user's expired tokens so the table does not
    # grow without bound. Cheap here because it is scoped to one user and
    # indexed; a scheduled sweep would handle abandoned accounts.
    db.execute(
        delete(AuthToken).where(
            AuthToken.user_id == user.id, AuthToken.expires_at <= datetime.now(timezone.utc)
        )
    )

    token, ttl = issue_token(db, user)
    return TokenOut(access_token=token, expires_in=ttl)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    caller: User = Depends(current_user),
) -> None:
    """Revoke the presented token.

    Without this a leaked token stays valid for its full TTL with no way to
    cut it off.
    """
    assert creds is not None  # current_user already rejected the None case
    db.execute(
        delete(AuthToken).where(
            AuthToken.user_id == caller.id,
            AuthToken.token_hash == token_digest(creds.credentials),
        )
    )
    db.commit()
