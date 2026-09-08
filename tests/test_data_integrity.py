"""API-versus-database consistency, checked with SQL.

The functional suite asks whether the API answered correctly. These tests ask
a different question: does the number the API reported actually match what was
written to the database, and does the ledger add up on its own terms? A system
can pass every endpoint test and still have a balance that disagrees with the
sum of its transactions.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.models import Portfolio, Transaction, User

pytestmark = pytest.mark.integrity


def _post(client, headers, kind, amount):
    response = client.post(
        "/transactions", json={"type": kind, "amount": amount}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_api_balance_equals_database_balance(client, user, db_session):
    reported = _post(client, user["headers"], "deposit", "50000.00")

    stored = db_session.execute(
        select(User.balance).where(User.id == user["account"]["id"])
    ).scalar_one()

    assert Decimal(stored) == Decimal(reported["balance_after"]), (
        "the balance the API returned is not the balance that was persisted"
    )


def test_balance_equals_sum_of_ledger(client, user, db_session):
    """The invariant: balance == sum(deposits) - sum(withdrawals).

    Checked in SQL rather than by replaying the API calls, so a write that
    bypassed the balance update would still be caught.
    """
    for kind, amount in [
        ("deposit", "10000.00"),
        ("deposit", "25000.50"),
        ("withdrawal", "3000.25"),
        ("deposit", "0.01"),
        ("withdrawal", "1234.56"),
    ]:
        _post(client, user["headers"], kind, amount)

    user_id = user["account"]["id"]
    computed = db_session.execute(
        text(
            """
            SELECT COALESCE(SUM(
                CASE WHEN type = 'deposit' THEN amount ELSE -amount END
            ), 0)
            FROM transactions
            WHERE user_id = :uid
            """
        ),
        {"uid": user_id},
    ).scalar_one()

    stored = db_session.execute(
        select(User.balance).where(User.id == user_id)
    ).scalar_one()

    assert Decimal(str(computed)) == Decimal(stored), (
        f"ledger sums to {computed} but the account balance is {stored}"
    )


def test_each_balance_after_matches_the_running_total(client, user, db_session):
    """Every row's balance_after must equal the running total up to that row.

    This is what catches a lost update: two writes that each read the same
    starting balance produce rows whose balance_after skips a step.
    """
    amounts = [
        ("deposit", "1000.00"),
        ("deposit", "2000.00"),
        ("withdrawal", "500.00"),
        ("deposit", "333.33"),
        ("withdrawal", "0.33"),
    ]
    for kind, amount in amounts:
        _post(client, user["headers"], kind, amount)

    rows = list(
        db_session.execute(
            select(Transaction)
            .where(Transaction.user_id == user["account"]["id"])
            .order_by(Transaction.id)
        ).scalars()
    )
    assert len(rows) == len(amounts)

    running = Decimal("0.00")
    for row in rows:
        running += Decimal(row.amount) if row.type == "deposit" else -Decimal(row.amount)
        assert Decimal(row.balance_after) == running, (
            f"transaction {row.id}: balance_after is {row.balance_after}, "
            f"running total is {running}"
        )


def test_money_survives_the_round_trip_without_precision_loss(client, user, db_session):
    """Amounts with awkward decimal parts must come back exactly.

    0.1 and 0.7 have no exact binary float representation; if anything in the
    stack drops to float these assertions fail by a paisa.
    """
    awkward = ["1000.10", "2000.70", "3333.33", "9999.99", "1000.01"]
    for amount in awkward:
        _post(client, user["headers"], "deposit", amount)

    rows = list(
        db_session.execute(
            select(Transaction.amount)
            .where(Transaction.user_id == user["account"]["id"])
            .order_by(Transaction.id)
        ).scalars()
    )
    assert [Decimal(r) for r in rows] == [Decimal(a) for a in awkward]

    expected_total = sum(Decimal(a) for a in awkward)
    stored = db_session.execute(
        select(User.balance).where(User.id == user["account"]["id"])
    ).scalar_one()
    assert Decimal(stored) == expected_total


def test_no_transaction_row_has_a_non_positive_amount(client, funded_user, db_session):
    """A negative amount row would let a withdrawal masquerade as a deposit."""
    client.post(
        "/transactions",
        json={"type": "deposit", "amount": "-5000"},
        headers=funded_user["headers"],
    )
    offenders = db_session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.amount <= 0)
    ).scalar_one()
    assert offenders == 0


def test_no_orphaned_rows(client, user, db_session):
    """Every portfolio and transaction must point at a user that exists."""
    client.post(
        "/portfolio",
        json={
            "name": "Retirement",
            "asset_type": "equity_index",
            "amount": "50000",
            "horizon_years": 10,
        },
        headers=user["headers"],
    )
    _post(client, user["headers"], "deposit", "1000.00")

    orphan_portfolios = db_session.execute(
        text(
            "SELECT COUNT(*) FROM portfolios p "
            "LEFT JOIN users u ON u.id = p.user_id WHERE u.id IS NULL"
        )
    ).scalar_one()
    orphan_transactions = db_session.execute(
        text(
            "SELECT COUNT(*) FROM transactions t "
            "LEFT JOIN users u ON u.id = t.user_id WHERE u.id IS NULL"
        )
    ).scalar_one()

    assert orphan_portfolios == 0
    assert orphan_transactions == 0


def test_no_duplicate_idempotency_keys_per_user(client, user, db_session):
    for _ in range(3):
        client.post(
            "/transactions",
            json={"type": "deposit", "amount": "1000.00"},
            headers={**user["headers"], "Idempotency-Key": "same-key"},
        )

    duplicates = db_session.execute(
        text(
            """
            SELECT COUNT(*) FROM (
                SELECT user_id, idempotency_key
                FROM transactions
                WHERE idempotency_key IS NOT NULL
                GROUP BY user_id, idempotency_key
                HAVING COUNT(*) > 1
            ) dupes
            """
        )
    ).scalar_one()
    assert duplicates == 0


def test_portfolio_amount_persists_exactly_as_submitted(client, user, db_session):
    submitted = "12345.67"
    created = client.post(
        "/portfolio",
        json={
            "name": "Precision check",
            "asset_type": "debt_fund",
            "amount": submitted,
            "horizon_years": 7,
        },
        headers=user["headers"],
    ).json()

    stored = db_session.execute(
        select(Portfolio.amount).where(Portfolio.id == created["id"])
    ).scalar_one()

    assert Decimal(stored) == Decimal(submitted)
    assert Decimal(created["amount"]) == Decimal(submitted)


def test_sql_injection_in_a_text_field_is_stored_inertly(client, user, db_session):
    """The payload must land in the row as text, and the table must survive."""
    payload = "'; DROP TABLE transactions; --"
    created = client.post(
        "/portfolio",
        json={
            "name": payload,
            "asset_type": "gold",
            "amount": "50000",
            "horizon_years": 3,
        },
        headers=user["headers"],
    )
    assert created.status_code == 201
    assert created.json()["name"] == payload

    # The table is still there and still queryable.
    assert db_session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 0
    stored = db_session.execute(
        select(Portfolio.name).where(Portfolio.id == created.json()["id"])
    ).scalar_one()
    assert stored == payload
