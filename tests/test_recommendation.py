"""The recommendation endpoint.

Two things are under test and they are separable: the arithmetic, which must be
exactly right, and the explanation, which must never contradict or outrun the
arithmetic. The scripted provider supplies the kind of output a real model
occasionally produces so the guardrail path can be exercised on demand.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import EXPECTED_RETURN_PCT
from app.finance import project_value, suggest_asset_type
from app.llm.prompts import SYSTEM_CANARY
from app.models import Recommendation
from tests.conftest import auth_header, login, register


def recommend(client, headers, **kwargs):
    payload = {"amount": "50000", "horizon_years": 5, **kwargs}
    return client.post("/recommendation", json=payload, headers=headers)


# --- arithmetic -----------------------------------------------------------


@pytest.mark.functional
def test_projection_is_arithmetically_correct(client, user):
    """50,000 at 9.5% for 5 years. Computed independently of the application."""
    response = recommend(client, user["headers"], amount="50000", horizon_years=5)
    assert response.status_code == 200
    body = response.json()

    rate = Decimal(body["expected_return_pct"]) / 100
    expected = (Decimal("50000") * (1 + rate) ** 5).quantize(Decimal("0.01"))
    assert Decimal(body["projected_value"]) == expected


@pytest.mark.functional
@pytest.mark.parametrize("asset_type", sorted(EXPECTED_RETURN_PCT))
def test_projection_correct_for_every_asset_type(client, user, asset_type):
    response = recommend(client, user["headers"], asset_type=asset_type)
    body = response.json()
    assert body["asset_type"] == asset_type
    assert Decimal(body["expected_return_pct"]) == EXPECTED_RETURN_PCT[asset_type]
    assert Decimal(body["projected_value"]) == project_value(
        Decimal("50000"), EXPECTED_RETURN_PCT[asset_type], 5
    )


@pytest.mark.boundary
def test_one_year_horizon_applies_the_rate_exactly_once(client, user):
    response = recommend(
        client, user["headers"], amount="100000", horizon_years=1, asset_type="fixed_deposit"
    )
    body = response.json()
    # 100000 * 1.065 = 106500.00, with no compounding beyond the single year.
    assert Decimal(body["projected_value"]) == Decimal("106500.00")


@pytest.mark.functional
def test_projection_grows_monotonically_with_horizon(client, user):
    values = []
    for years in (1, 5, 10, 20, 40):
        body = recommend(
            client, user["headers"], horizon_years=years, asset_type="equity_index"
        ).json()
        values.append(Decimal(body["projected_value"]))
    assert values == sorted(values), "a longer horizon produced a smaller projection"


@pytest.mark.functional
def test_projected_value_never_below_principal_for_positive_rates(client, user):
    for asset_type in EXPECTED_RETURN_PCT:
        body = recommend(client, user["headers"], asset_type=asset_type).json()
        assert Decimal(body["projected_value"]) > Decimal(body["amount"])


# --- asset selection ------------------------------------------------------


@pytest.mark.functional
@pytest.mark.parametrize("risk_score", range(1, 11))
def test_asset_suggestion_matches_risk_appetite(client, risk_score):
    email = f"risk{risk_score}@example.com"
    register(client, email, risk_score=risk_score)
    headers = auth_header(login(client, email))

    body = recommend(client, headers).json()
    assert body["risk_score"] == risk_score
    assert body["asset_type"] == suggest_asset_type(risk_score)


@pytest.mark.functional
def test_a_conservative_user_is_not_pushed_into_equity(client):
    register(client, "cautious@example.com", risk_score=1)
    headers = auth_header(login(client, "cautious@example.com"))
    body = recommend(client, headers).json()
    assert body["asset_type"] == "fixed_deposit"


@pytest.mark.functional
def test_explicit_asset_type_overrides_the_suggestion(client, user):
    """The user asked for gold; risk score 7 would have suggested hybrid."""
    body = recommend(client, user["headers"], asset_type="gold").json()
    assert body["asset_type"] == "gold"


# --- response contract ----------------------------------------------------


@pytest.mark.schema
def test_response_contract(client, user):
    body = recommend(client, user["headers"]).json()

    required = {
        "id", "user_id", "amount", "horizon_years", "asset_type", "risk_score",
        "expected_return_pct", "projected_value", "explanation", "disclaimer",
        "model", "created_at",
    }
    assert required <= set(body)

    assert isinstance(body["id"], int)
    assert isinstance(body["user_id"], int)
    assert isinstance(body["horizon_years"], int)
    assert isinstance(body["explanation"], str) and body["explanation"].strip()
    assert isinstance(body["disclaimer"], str) and body["disclaimer"].strip()
    assert 1 <= body["risk_score"] <= 10
    assert Decimal(body["projected_value"]) > 0
    assert Decimal(body["expected_return_pct"]) > 0


@pytest.mark.schema
def test_disclaimer_is_always_present(client, user):
    for asset_type in EXPECTED_RETURN_PCT:
        body = recommend(client, user["headers"], asset_type=asset_type).json()
        assert "do not guarantee returns" in body["disclaimer"]


@pytest.mark.security
def test_recommendation_requires_authentication(client):
    assert client.post("/recommendation", json={"amount": "50000", "horizon_years": 5}).status_code == 401


@pytest.mark.integrity
def test_recommendation_is_persisted_as_returned(client, user, db_session):
    body = recommend(client, user["headers"]).json()
    row = db_session.execute(
        select(Recommendation).where(Recommendation.id == body["id"])
    ).scalar_one()

    assert Decimal(row.projected_value) == Decimal(body["projected_value"])
    assert Decimal(row.expected_return_pct) == Decimal(body["expected_return_pct"])
    assert row.asset_type == body["asset_type"]
    assert row.explanation == body["explanation"]


# --- guardrail behaviour at the endpoint ---------------------------------


@pytest.mark.llm
def test_explanation_quotes_only_the_supplied_rate(client, user):
    body = recommend(client, user["headers"], asset_type="equity_index").json()
    # The one percentage the model was given is 12.0; nothing else may appear.
    import re

    quoted = {Decimal(m).normalize() for m in re.findall(r"(\d+(?:\.\d+)?)\s*%", body["explanation"])}
    assert quoted <= {Decimal("12.0").normalize()}


@pytest.mark.llm
def test_hallucinated_rate_is_suppressed(client, user, scripted_llm):
    """A model that invents a rate must not reach the user."""
    scripted_llm.responses = ["This will return 30% a year, easily."]
    body = recommend(client, user["headers"], asset_type="equity_index").json()

    assert "30%" not in body["explanation"]
    assert body["model"].endswith("+fallback")
    assert "12.0%" in body["explanation"] or "12.0" in body["explanation"]


@pytest.mark.llm
def test_guaranteed_return_claim_is_suppressed(client, user, scripted_llm):
    scripted_llm.responses = ["Equity index funds offer guaranteed returns of 12.0% a year."]
    body = recommend(client, user["headers"], asset_type="equity_index").json()

    assert "guarantee" not in body["explanation"].lower()
    assert body["model"].endswith("+fallback")


@pytest.mark.llm
def test_leaked_system_prompt_is_suppressed(client, user, scripted_llm):
    scripted_llm.responses = [f"My policy reference is {SYSTEM_CANARY}, and the rate is 12.0%."]
    body = recommend(client, user["headers"], asset_type="equity_index").json()

    assert SYSTEM_CANARY not in body["explanation"]
    assert body["model"].endswith("+fallback")


@pytest.mark.llm
def test_empty_generation_falls_back(client, user, scripted_llm):
    scripted_llm.responses = ["   "]
    body = recommend(client, user["headers"]).json()
    assert body["explanation"].strip()
    assert body["model"].endswith("+fallback")


@pytest.mark.llm
def test_model_refusal_falls_back_rather_than_erroring(client, user, scripted_llm):
    """A safety decline must not surface as a 500."""
    scripted_llm.responses = ["__REFUSAL__"]
    response = recommend(client, user["headers"])
    assert response.status_code == 200
    assert response.json()["explanation"].strip()
    assert response.json()["model"].endswith("+fallback")


@pytest.mark.llm
def test_fallback_text_still_states_the_correct_figures(client, user, scripted_llm):
    """The substituted text is not a placeholder - it carries the real numbers."""
    scripted_llm.responses = ["Guaranteed 40% returns!"]
    body = recommend(
        client, user["headers"], amount="50000", horizon_years=5, asset_type="equity_index"
    ).json()

    assert body["projected_value"] in body["explanation"]
    assert "12.0%" in body["explanation"]
    assert "40%" not in body["explanation"]


@pytest.mark.llm
def test_model_is_never_asked_to_do_the_arithmetic(client, user, scripted_llm):
    """The prompt must hand the model a finished projection, not ask for one."""
    scripted_llm.responses = ["A market-linked estimate at 12.0% a year."]
    recommend(client, user["headers"], asset_type="equity_index")

    _system, prompt = scripted_llm.calls[0]
    assert "projected_value:" in prompt
    assert "expected_return_pct:" in prompt
    assert "calculate" not in prompt.lower()
    assert "compute" not in prompt.lower()


@pytest.mark.llm
def test_a_clean_generation_is_passed_through_unchanged(client, user, scripted_llm):
    """The guardrail must not be a blanket suppressor."""
    clean = "Equity is market-linked; the 12.0% figure is a long-run average, not a promise."
    scripted_llm.responses = [clean]
    body = recommend(client, user["headers"], asset_type="equity_index").json()

    assert body["explanation"] == clean
    assert not body["model"].endswith("+fallback")
