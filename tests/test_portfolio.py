"""Portfolio CRUD and tenant isolation."""
from __future__ import annotations

from decimal import Decimal

import pytest

pytestmark = pytest.mark.functional


def make(client, headers, **kwargs):
    payload = {
        "name": "Retirement",
        "asset_type": "equity_index",
        "amount": "50000",
        "horizon_years": 10,
        **kwargs,
    }
    return client.post("/portfolio", json=payload, headers=headers)


def test_create_returns_201_with_the_stored_values(client, user):
    response = make(client, user["headers"])
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Retirement"
    assert body["asset_type"] == "equity_index"
    assert Decimal(body["amount"]) == Decimal("50000")
    assert body["horizon_years"] == 10
    assert body["user_id"] == user["account"]["id"]


def test_created_portfolio_can_be_read_back(client, user):
    created = make(client, user["headers"]).json()
    response = client.get(f"/portfolio/{created['id']}", headers=user["headers"])
    assert response.status_code == 200
    assert response.json() == created


def test_list_returns_only_the_callers_holdings(client, user, other_user):
    make(client, user["headers"], name="Mine A")
    make(client, user["headers"], name="Mine B")
    make(client, other_user["headers"], name="Theirs")

    mine = client.get("/portfolio", headers=user["headers"]).json()
    assert {p["name"] for p in mine} == {"Mine A", "Mine B"}
    assert all(p["user_id"] == user["account"]["id"] for p in mine)


def test_list_is_empty_for_a_new_account(client, user):
    response = client.get("/portfolio", headers=user["headers"])
    assert response.status_code == 200
    assert response.json() == []


def test_list_is_ordered_deterministically(client, user):
    names = [f"Holding {i}" for i in range(5)]
    for name in names:
        make(client, user["headers"], name=name)
    listed = [p["name"] for p in client.get("/portfolio", headers=user["headers"]).json()]
    assert listed == names


@pytest.mark.negative
def test_unknown_portfolio_returns_404(client, user):
    assert client.get("/portfolio/999999", headers=user["headers"]).status_code == 404


@pytest.mark.negative
def test_non_integer_portfolio_id_returns_422(client, user):
    assert client.get("/portfolio/abc", headers=user["headers"]).status_code == 422


@pytest.mark.security
def test_create_requires_authentication(client):
    response = client.post(
        "/portfolio",
        json={
            "name": "Anon",
            "asset_type": "gold",
            "amount": "50000",
            "horizon_years": 3,
        },
    )
    assert response.status_code == 401


@pytest.mark.security
def test_cannot_read_another_users_portfolio(client, user, other_user):
    created = make(client, user["headers"]).json()
    response = client.get(f"/portfolio/{created['id']}", headers=other_user["headers"])
    assert response.status_code == 404


def test_multiple_holdings_of_the_same_asset_are_allowed(client, user):
    """A user may hold the same asset class in two goals."""
    first = make(client, user["headers"], name="Goal A", asset_type="gold")
    second = make(client, user["headers"], name="Goal B", asset_type="gold")
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.integrity
def test_creating_a_portfolio_does_not_move_money(client, funded_user):
    """A portfolio is a plan, not a transfer. The balance must not change."""
    before = client.get("/transactions", headers=funded_user["headers"]).json()
    make(client, funded_user["headers"], amount="50000")
    after = client.get("/transactions", headers=funded_user["headers"]).json()
    assert before == after


@pytest.mark.boundary
def test_portfolio_amount_is_independent_of_account_balance(client, user):
    """A user with no balance can still model a 50,000 investment.

    Coupling the two would be a business-rule change, not a bug fix - this test
    pins the current, intended behaviour so the coupling cannot appear by
    accident.
    """
    assert Decimal(user["account"]["balance"]) == 0
    assert make(client, user["headers"], amount="50000").status_code == 201
