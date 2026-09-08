from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.finance import round_money
from app.models import Transaction, User
from app.schemas import TransactionCreate, TransactionOut
from app.security import current_user

router = APIRouter(tags=["transactions"])


@router.post(
    "/transactions", response_model=TransactionOut, status_code=status.HTTP_201_CREATED
)
def create_transaction(
    payload: TransactionCreate,
    db: Session = Depends(get_db),
    caller: User = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Transaction:
    if idempotency_key is not None:
        existing = db.execute(
            select(Transaction).where(
                Transaction.user_id == caller.id,
                Transaction.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            # A retry of a request that already succeeded returns the original
            # row rather than moving money a second time.
            return existing

    # Re-read the user inside this transaction. On Postgres take a row lock so
    # two concurrent posts cannot both read the same starting balance; sqlite
    # serialises writes at the file level and does not support FOR UPDATE.
    stmt = select(User).where(User.id == caller.id)
    if db.bind is not None and db.bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    user = db.execute(stmt).scalar_one()

    amount = Decimal(payload.amount)
    balance = Decimal(user.balance)

    if payload.type == "withdrawal":
        if amount > balance:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Insufficient balance",
            )
        new_balance = balance - amount
    else:
        new_balance = balance + amount

    new_balance = round_money(new_balance)
    user.balance = new_balance

    txn = Transaction(
        user_id=user.id,
        type=payload.type,
        amount=round_money(amount),
        balance_after=new_balance,
        idempotency_key=idempotency_key,
    )
    db.add(txn)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent retries of the same key: one wins, the other returns
        # the winner's row instead of erroring.
        db.rollback()
        existing = db.execute(
            select(Transaction).where(
                Transaction.user_id == caller.id,
                Transaction.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        raise
    db.refresh(txn)
    return txn


@router.get("/transactions", response_model=list[TransactionOut])
def list_transactions(
    db: Session = Depends(get_db),
    caller: User = Depends(current_user),
    limit: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    offset: int = Query(default=0, ge=0),
) -> list[Transaction]:
    """Paged, oldest first - a ledger is read forwards, and a stable order is
    what makes offset paging safe here."""
    return list(
        db.execute(
            select(Transaction)
            .where(Transaction.user_id == caller.id)
            .order_by(Transaction.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
