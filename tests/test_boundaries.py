"""Boundary-value and equivalence-partition tests.

The investment amount rule is 1,000 <= amount <= 100,000. Each bound is probed
at the value itself, one paisa either side, and one whole rupee either side,
because off-by-one at a boundary is the defect this class of test exists to
catch. Malformed inputs are checked separately: the requirement there is that
they are rejected, never silently coerced.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from qa.test_data import (
    AMOUNT_BOUNDARIES,
    AMOUNT_MALFORMED,
    AMOUNT_VALID_STRING_FORMS,
    HORIZON_BOUNDARIES,
    HORIZON_MALFORMED,
    INVALID_ASSET_TYPES,
    RISK_BOUNDARIES,
)

pytestmark = pytest.mark.boundary


def _portfolio(amount, horizon=5, asset_type="equity_index", name="Boundary probe"):
    return {
        "name": name,
        "asset_type": asset_type,
        "amount": amount,
        "horizon_years": horizon,
    }


@pytest.mark.parametrize(
    "amount,accepted,label",
    AMOUNT_BOUNDARIES,
    ids=[label for _, _, label in AMOUNT_BOUNDARIES],
)
def test_investment_amount_boundaries(client, user, amount, accepted, label):
    response = client.post(
        "/portfolio", json=_portfolio(amount), headers=user["headers"]
    )
    if accepted:
        assert response.status_code == 201, (
            f"{label}: {amount} is inside the allowed range but was rejected "
            f"({response.status_code}) - {response.text}"
        )
        assert Decimal(response.json()["amount"]) == Decimal(amount)
    else:
        assert response.status_code == 422, (
            f"{label}: {amount} is outside the allowed range but was accepted "
            f"({response.status_code})"
        )


@pytest.mark.parametrize(
    "amount,label", AMOUNT_MALFORMED, ids=[label for _, label in AMOUNT_MALFORMED]
)
def test_malformed_amount_is_rejected_not_coerced(client, user, amount, label):
    """A non-numeric amount must 422.

    The failure this guards against is coercion: `bool(True)` becoming 1, an
    empty string becoming 0, or "50,000" being parsed as 50. Any of those
    writes a wrong number into a financial record while returning 201.
    """
    response = client.post(
        "/portfolio", json=_portfolio(amount), headers=user["headers"]
    )
    assert response.status_code == 422, (
        f"{label}: expected 422, got {response.status_code}. Body: {response.text[:200]}"
    )


@pytest.mark.parametrize("amount", AMOUNT_VALID_STRING_FORMS)
def test_money_accepted_as_string(client, user, amount):
    """Clients send money as a JSON string to dodge float precision loss."""
    response = client.post(
        "/portfolio", json=_portfolio(amount), headers=user["headers"]
    )
    assert response.status_code == 201, response.text
    assert Decimal(response.json()["amount"]) == Decimal(amount)


def test_float_amount_keeps_exact_paisa(client, user):
    """0.1 + 0.2 problems must not reach the ledger.

    Sent as a JSON float, 50000.1 has no exact binary representation. What
    comes back must still be 50000.10 to the paisa.
    """
    response = client.post(
        "/portfolio", json=_portfolio(50000.1), headers=user["headers"]
    )
    assert response.status_code == 201, response.text
    assert Decimal(response.json()["amount"]) == Decimal("50000.10")


@pytest.mark.parametrize(
    "years,accepted,label",
    HORIZON_BOUNDARIES,
    ids=[label for _, _, label in HORIZON_BOUNDARIES],
)
def test_horizon_boundaries(client, user, years, accepted, label):
    response = client.post(
        "/portfolio", json=_portfolio("50000", horizon=years), headers=user["headers"]
    )
    expected = 201 if accepted else 422
    assert response.status_code == expected, f"{label}: horizon {years}"


@pytest.mark.parametrize(
    "years,label", HORIZON_MALFORMED, ids=[label for _, label in HORIZON_MALFORMED]
)
def test_malformed_horizon_is_rejected(client, user, years, label):
    response = client.post(
        "/portfolio", json=_portfolio("50000", horizon=years), headers=user["headers"]
    )
    assert response.status_code == 422, f"{label}: expected 422, got {response.status_code}"


@pytest.mark.parametrize(
    "score,accepted,label",
    RISK_BOUNDARIES,
    ids=[label for _, _, label in RISK_BOUNDARIES],
)
def test_risk_score_boundaries(client, score, accepted, label):
    response = client.post(
        "/users",
        json={
            "email": f"risk{abs(score)}{'neg' if score < 0 else ''}@example.com",
            "full_name": "Risk Probe",
            "password": "correct-horse-9",
            "risk_score": score,
        },
    )
    expected = 201 if accepted else 422
    assert response.status_code == expected, f"{label}: risk_score {score}"


@pytest.mark.parametrize(
    "asset,label", INVALID_ASSET_TYPES, ids=[label for _, label in INVALID_ASSET_TYPES]
)
def test_invalid_asset_type_rejected(client, user, asset, label):
    response = client.post(
        "/portfolio", json=_portfolio("50000", asset_type=asset), headers=user["headers"]
    )
    assert response.status_code == 422, f"{label}: expected 422, got {response.status_code}"


def test_unknown_field_is_rejected(client, user):
    """A typo'd or extra field means the client and server disagree.

    Accepting it silently writes a row missing the value the caller thought
    they set.
    """
    payload = _portfolio("50000")
    payload["ammount"] = "999999"
    response = client.post("/portfolio", json=payload, headers=user["headers"])
    assert response.status_code == 422


def test_missing_required_fields_rejected(client, user):
    response = client.post("/portfolio", json={}, headers=user["headers"])
    assert response.status_code == 422
    missing = {
        tuple(err["loc"])[-1] for err in response.json()["detail"] if err["type"] == "missing"
    }
    assert {"name", "asset_type", "amount", "horizon_years"} <= missing
