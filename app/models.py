"""ORM models.

Money is NUMERIC(18, 2) everywhere and is carried as Decimal in Python. Storing
rupees in a float is the classic source of the "off by one paisa" defect the
data-integrity suite hunts for, so the type is pinned at the schema level.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

MONEY = Numeric(18, 2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    risk_score: Mapped[int] = mapped_column(Integer)
    balance: Mapped[Decimal] = mapped_column(MONEY, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    portfolios: Mapped[list["Portfolio"]] = relationship(back_populates="user")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")

    __table_args__ = (
        CheckConstraint("risk_score >= 1 AND risk_score <= 10", name="ck_users_risk_score"),
    )


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    asset_type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    horizon_years: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="portfolios")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_portfolios_amount_positive"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(16))  # deposit | withdrawal
    amount: Mapped[Decimal] = mapped_column(MONEY)
    balance_after: Mapped[Decimal] = mapped_column(MONEY)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="transactions")

    __table_args__ = (
        # A retried POST must not create a second row. Scoped per user so two
        # users generating the same client-side key don't collide.
        UniqueConstraint("user_id", "idempotency_key", name="uq_txn_user_idempotency"),
        CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
    )


class AuthToken(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    horizon_years: Mapped[int] = mapped_column(Integer)
    asset_type: Mapped[str] = mapped_column(String(32))
    expected_return_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    projected_value: Mapped[Decimal] = mapped_column(MONEY)
    explanation: Mapped[str] = mapped_column(String(4000))
    model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
