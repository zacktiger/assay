"""Recommendation and chat - the two LLM-backed endpoints.

The shape of both is the same: compute everything first, ask the model only to
put words around the result, then validate what comes back. If validation
fails the user gets a deterministic fallback, never the unvalidated text.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.finance import expected_return_pct, project_value, suggest_asset_type
from app.llm import get_provider
from app.llm.guardrails import check
from app.llm.prompts import ADVISOR_SYSTEM, CHAT_SYSTEM, DISCLAIMER
from app.models import Recommendation, User
from app.schemas import ChatOut, ChatRequest, RecommendationOut, RecommendationRequest
from app.security import current_user

router = APIRouter(tags=["advisory"])


def _fallback_explanation(
    amount: Decimal, asset_type: str, years: int, pct: Decimal, projected: Decimal
) -> str:
    """Used when model output fails validation. Says only what was computed."""
    readable_asset = asset_type.replace("_", " ")
    return (
        f"{amount} rupees in {readable_asset} over {years} years projects to "
        f"{projected} rupees at a long-run average of {pct}% a year. This is an "
        "illustration based on historical asset-class averages, not a forecast; "
        "market-linked returns vary and can be negative."
    )


@router.post("/recommendation", response_model=RecommendationOut)
def recommend(
    payload: RecommendationRequest,
    db: Session = Depends(get_db),
    caller: User = Depends(current_user),
) -> RecommendationOut:
    asset_type = payload.asset_type or suggest_asset_type(caller.risk_score)
    pct = expected_return_pct(asset_type)
    projected = project_value(payload.amount, pct, payload.horizon_years)

    prompt = (
        "Explain the following figures to the investor in plain language.\n"
        f"- amount: {payload.amount}\n"
        f"- asset_type: {asset_type}\n"
        f"- horizon_years: {payload.horizon_years}\n"
        f"- expected_return_pct: {pct}\n"
        f"- projected_value: {projected}\n"
        f"- risk_score: {caller.risk_score}\n"
    )

    result = get_provider().complete(ADVISOR_SYSTEM, prompt)

    # The only percentage the model was given is the expected return. Any other
    # percentage in the text is invented.
    report = check(result.text, allowed_percentages={pct})
    if result.refused or not report.ok:
        explanation = _fallback_explanation(
            payload.amount, asset_type, payload.horizon_years, pct, projected
        )
        model_used = f"{result.model}+fallback"
    else:
        explanation = result.text.strip()
        model_used = result.model

    row = Recommendation(
        user_id=caller.id,
        amount=payload.amount,
        horizon_years=payload.horizon_years,
        asset_type=asset_type,
        expected_return_pct=pct,
        projected_value=projected,
        explanation=explanation,
        model=model_used,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return RecommendationOut(
        id=row.id,
        user_id=row.user_id,
        amount=row.amount,
        horizon_years=row.horizon_years,
        asset_type=row.asset_type,
        risk_score=caller.risk_score,
        expected_return_pct=row.expected_return_pct,
        projected_value=row.projected_value,
        explanation=row.explanation,
        disclaimer=DISCLAIMER,
        model=row.model,
        created_at=row.created_at,
    )


@router.post("/chat", response_model=ChatOut)
def chat(payload: ChatRequest, caller: User = Depends(current_user)) -> ChatOut:
    result = get_provider().complete(CHAT_SYSTEM, payload.message)

    # No figures were supplied, so no percentage in the reply can be sourced -
    # any number here would be the model's own invention.
    report = check(result.text, allowed_percentages=set())
    if result.refused or not report.ok:
        return ChatOut(
            reply=(
                "I can't give a reliable answer to that. For anything specific to "
                "your finances, please speak to a SEBI-registered adviser."
            ),
            model=f"{result.model}+fallback",
            refused=True,
        )
    return ChatOut(reply=result.text.strip(), model=result.model)
