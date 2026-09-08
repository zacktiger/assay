from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Portfolio, User
from app.schemas import PortfolioCreate, PortfolioOut
from app.security import current_user

router = APIRouter(tags=["portfolio"])


@router.post("/portfolio", response_model=PortfolioOut, status_code=status.HTTP_201_CREATED)
def create_portfolio(
    payload: PortfolioCreate,
    db: Session = Depends(get_db),
    caller: User = Depends(current_user),
) -> Portfolio:
    holding = Portfolio(
        user_id=caller.id,
        name=payload.name,
        asset_type=payload.asset_type,
        amount=payload.amount,
        horizon_years=payload.horizon_years,
    )
    db.add(holding)
    db.commit()
    db.refresh(holding)
    return holding


@router.get("/portfolio", response_model=list[PortfolioOut])
def list_portfolios(
    db: Session = Depends(get_db),
    caller: User = Depends(current_user),
    limit: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    offset: int = Query(default=0, ge=0),
) -> list[Portfolio]:
    """Paged. An unbounded list endpoint lets one caller pull the whole table
    into memory, so `limit` is capped by MAX_PAGE_SIZE rather than trusted."""
    return list(
        db.execute(
            select(Portfolio)
            .where(Portfolio.user_id == caller.id)
            .order_by(Portfolio.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


@router.get("/portfolio/{portfolio_id}", response_model=PortfolioOut)
def get_portfolio(
    portfolio_id: int,
    db: Session = Depends(get_db),
    caller: User = Depends(current_user),
) -> Portfolio:
    holding = db.get(Portfolio, portfolio_id)
    # Ownership is checked before existence is admitted, for the same reason as
    # the user endpoint: a bare 403 confirms the row exists.
    if holding is None or holding.user_id != caller.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio not found"
        )
    return holding
