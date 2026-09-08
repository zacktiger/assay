"""Transaction ledger behaviour: balances, overdrafts, retries."""
from __future__ import annotations

from decimal import Decimal

import pytest

pytestmark = pytest.mark.functional


def deposit(client, headers, amount, key=None):
    extra = {"Idempotency-Key": key} if key else {}
    return client.post(
        "/transactions",
        json={"type": "deposit", "amount": amount},
        headers={**headers, **extra},
    )


def withdraw(client, headers, amount, key=None):
    extra = {"Idempotency-Key": key} if key else {}
    return client.post(
        "/transactions",
        json={"type": "withdrawal", "amount": amount},
        headers={**headers, **extra},
    )


def test_deposit_increases_balance(client, user):
    response = deposit(client, user["headers"], "10000.00")
    assert response.status_code == 201
    assert Decimal(response.json()["balance_after"]) == Decimal("10000.00")


def test_withdrawal_decreases_balance(client, funded_user):
    response = withdraw(client, funded_user["headers"], "20000.00")
    assert response.status_code == 201
    assert Decimal(response.json()["balance_after"]) == Decimal("30000.00")


def test_running_balance_is_correct_across_a_sequence(client, user):
    """Balance after each step must equal the running total, to the paisa."""
    steps = [
        ("deposit", "10000.00", "10000.00"),
        ("deposit", "2500.50", "12500.50"),
        ("withdrawal", "500.25", "12000.25"),
        ("deposit", "0.01", "12000.26"),
        ("withdrawal", "12000.26", "0.00"),
    ]
    for kind, amount, expected in steps:
        response = client.post(
            "/transactions",
            json={"type": kind, "amount": amount},
            headers=user["headers"],
        )
        assert response.status_code == 201, response.text
        actual = Decimal(response.json()["balance_after"])
        assert actual == Decimal(expected), (
            f"after {kind} of {amount}: expected {expected}, got {actual}"
        )


@pytest.mark.negative
def test_overdraft_is_refused(client, funded_user):
    response = withdraw(client, funded_user["headers"], "50000.01")
    assert response.status_code == 422
    assert "insufficient" in response.json()["detail"].lower()


@pytest.mark.negative
def test_refused_overdraft_leaves_balance_untouched(client, funded_user, db_session):
    withdraw(client, funded_user["headers"], "99999.00")
    listed = client.get("/transactions", headers=funded_user["headers"]).json()
    assert len(listed) == 1, "the refused withdrawal was still written to the ledger"
    assert Decimal(listed[-1]["balance_after"]) == Decimal("50000.00")


@pytest.mark.boundary
def test_withdrawing_the_exact_balance_is_allowed(client, funded_user):
    response = withdraw(client, funded_user["headers"], "50000.00")
    assert response.status_code == 201
    assert Decimal(response.json()["balance_after"]) == Decimal("0.00")


@pytest.mark.boundary
@pytest.mark.parametrize("amount", ["0", "0.00", "-100", "-0.01"])
def test_non_positive_amounts_are_rejected(client, funded_user, amount):
    """A zero or negative deposit is either a client bug or an attempt to
    withdraw through the deposit path."""
    response = deposit(client, funded_user["headers"], amount)
    assert response.status_code == 422, f"amount {amount} was accepted"


@pytest.mark.negative
def test_unknown_transaction_type_rejected(client, user):
    response = client.post(
        "/transactions",
        json={"type": "transfer", "amount": "1000"},
        headers=user["headers"],
    )
    assert response.status_code == 422


@pytest.mark.security
def test_transactions_require_authentication(client):
    assert client.post("/transactions", json={"type": "deposit", "amount": "1"}).status_code == 401
    assert client.get("/transactions").status_code == 401


@pytest.mark.security
def test_users_only_see_their_own_transactions(client, funded_user, other_user):
    deposit(client, other_user["headers"], "7777.00")

    mine = client.get("/transactions", headers=funded_user["headers"]).json()
    theirs = client.get("/transactions", headers=other_user["headers"]).json()

    assert len(mine) == 1 and Decimal(mine[0]["amount"]) == Decimal("50000.00")
    assert len(theirs) == 1 and Decimal(theirs[0]["amount"]) == Decimal("7777.00")
    assert {t["user_id"] for t in mine} == {funded_user["account"]["id"]}


def test_retry_with_same_idempotency_key_does_not_double_charge(client, user):
    """The defect this catches: a client retries after a timeout and the money
    moves twice."""
    first = deposit(client, user["headers"], "10000.00", key="retry-abc-123")
    second = deposit(client, user["headers"], "10000.00", key="retry-abc-123")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"], "the retry created a second row"

    ledger = client.get("/transactions", headers=user["headers"]).json()
    assert len(ledger) == 1
    assert Decimal(ledger[0]["balance_after"]) == Decimal("10000.00")


def test_different_idempotency_keys_create_separate_transactions(client, user):
    deposit(client, user["headers"], "1000.00", key="key-one")
    deposit(client, user["headers"], "1000.00", key="key-two")
    ledger = client.get("/transactions", headers=user["headers"]).json()
    assert len(ledger) == 2
    assert Decimal(ledger[-1]["balance_after"]) == Decimal("2000.00")


def test_idempotency_keys_are_scoped_per_user(client, user, other_user):
    """Two users generating the same client-side key must not collide."""
    mine = deposit(client, user["headers"], "1000.00", key="shared-key")
    theirs = deposit(client, other_user["headers"], "2000.00", key="shared-key")

    assert mine.status_code == 201
    assert theirs.status_code == 201, "the second user's key collided with the first"
    assert Decimal(theirs.json()["balance_after"]) == Decimal("2000.00")


def test_transactions_without_a_key_are_not_deduplicated(client, user):
    """Absent a key the server cannot tell a retry from a genuine repeat, and
    must take the request at face value."""
    deposit(client, user["headers"], "1000.00")
    deposit(client, user["headers"], "1000.00")
    ledger = client.get("/transactions", headers=user["headers"]).json()
    assert len(ledger) == 2
