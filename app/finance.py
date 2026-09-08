"""Deterministic financial maths.

Every number the API returns is computed here, in Decimal, and never by the
language model. The LLM's only job is to explain numbers it was handed. That
split is what makes the recommendation endpoint testable: a projection is
either arithmetically right or it isn't, independent of model output.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.config import ASSET_RISK_LEVEL, EXPECTED_RETURN_PCT

PAISA = Decimal("0.01")


def round_money(value: Decimal) -> Decimal:
    """Half-up to two places - the convention Indian financial reporting uses.

    Python's default banker's rounding would make ledger totals disagree with
    the sum of their rows, which the integrity suite checks for.
    """
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


def suggest_asset_type(risk_score: int) -> str:
    """Pick the asset whose risk level sits closest to the user's appetite.

    Ties break toward the lower-risk asset, so a user is never pushed up the
    risk ladder by a rounding accident.
    """
    return min(
        ASSET_RISK_LEVEL,
        key=lambda asset: (abs(ASSET_RISK_LEVEL[asset] - risk_score), ASSET_RISK_LEVEL[asset]),
    )


def expected_return_pct(asset_type: str) -> Decimal:
    return EXPECTED_RETURN_PCT[asset_type]


def project_value(amount: Decimal, annual_return_pct: Decimal, years: int) -> Decimal:
    """Compound `amount` annually. Returns rupees, rounded once at the end."""
    rate = Decimal(1) + (annual_return_pct / Decimal(100))
    return round_money(amount * (rate**years))
