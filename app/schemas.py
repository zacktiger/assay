"""Request/response contracts.

Every bound here has a matching boundary test. `strict` numeric handling is
deliberate: the API accepts JSON numbers and numeric strings for money (clients
send both) but must reject "fifty thousand" with 422, never coerce it to 0.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.config import (
    ASSET_TYPES,
    MAX_HORIZON_YEARS,
    MAX_INVESTMENT,
    MAX_RISK_SCORE,
    MIN_HORIZON_YEARS,
    MIN_INVESTMENT,
    MIN_RISK_SCORE,
)

AssetType = Literal["equity_index", "debt_fund", "gold", "fixed_deposit", "hybrid"]

Money = Annotated[Decimal, Field(ge=MIN_INVESTMENT, le=MAX_INVESTMENT, decimal_places=2)]
RiskScore = Annotated[int, Field(ge=MIN_RISK_SCORE, le=MAX_RISK_SCORE)]
Horizon = Annotated[int, Field(ge=MIN_HORIZON_YEARS, le=MAX_HORIZON_YEARS)]


class _Strict(BaseModel):
    # Unknown keys are a client bug; surfacing them beats silently ignoring a
    # typo'd field and writing a half-populated row.
    model_config = ConfigDict(extra="forbid")


# --- users ----------------------------------------------------------------


class UserCreate(_Strict):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    risk_score: RiskScore

    @field_validator("full_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("full_name must not be blank")
        return v.strip()


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: str
    risk_score: int
    balance: Decimal
    created_at: datetime


class LoginRequest(_Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


# --- portfolio ------------------------------------------------------------


class PortfolioCreate(_Strict):
    name: str = Field(min_length=1, max_length=120)
    asset_type: AssetType
    amount: Money
    horizon_years: Horizon


class PortfolioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    asset_type: str
    amount: Decimal
    horizon_years: int
    created_at: datetime


# --- transactions ---------------------------------------------------------


class TransactionCreate(_Strict):
    type: Literal["deposit", "withdrawal"]
    amount: Annotated[Decimal, Field(gt=0, le=MAX_INVESTMENT, decimal_places=2)]


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    type: str
    amount: Decimal
    balance_after: Decimal
    created_at: datetime


# --- recommendation / chat ------------------------------------------------


class RecommendationRequest(_Strict):
    amount: Money
    horizon_years: Horizon
    asset_type: AssetType | None = None


class RecommendationOut(BaseModel):
    id: int
    user_id: int
    amount: Decimal
    horizon_years: int
    asset_type: str
    risk_score: int
    expected_return_pct: Decimal
    projected_value: Decimal
    explanation: str
    disclaimer: str
    model: str
    created_at: datetime


class ChatRequest(_Strict):
    message: str = Field(min_length=1, max_length=2000)


class ChatOut(BaseModel):
    reply: str
    model: str
    refused: bool = False


class ErrorOut(BaseModel):
    detail: str
