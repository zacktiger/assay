"""Password hashing and bearer-token auth.

pbkdf2-hmac-sha256 from the stdlib rather than a bcrypt dependency: it is
salted, iterated and constant-time verified, which is what the security suite
asserts. Tokens are random 32-byte secrets; only their SHA-256 digest is
stored, so a database dump does not hand over live sessions.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import AuthToken, User

_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"

bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def token_digest(token: str) -> str:
    """Only the digest is ever stored, so a database dump yields no live tokens."""
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(db: Session, user: User) -> tuple[str, int]:
    raw = secrets.token_urlsafe(32)
    ttl = settings.token_ttl_seconds
    db.add(
        AuthToken(
            user_id=user.id,
            token_hash=token_digest(raw),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
        )
    )
    db.commit()
    return raw, ttl


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if creds is None or not creds.credentials:
        raise _unauthorized()

    row = db.execute(
        select(AuthToken).where(AuthToken.token_hash == token_digest(creds.credentials))
    ).scalar_one_or_none()
    if row is None:
        raise _unauthorized("Invalid token")

    expires_at = row.expires_at
    if expires_at.tzinfo is None:  # sqlite hands back naive datetimes
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise _unauthorized("Token expired")

    user = db.get(User, row.user_id)
    if user is None:
        raise _unauthorized("Invalid token")
    return user
