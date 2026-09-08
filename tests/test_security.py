"""Injection payloads, error-message hygiene, and response-time budgets."""
from __future__ import annotations

import time
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.models import Portfolio, User
from qa.test_data import SQL_INJECTION_STRINGS, XSS_STRINGS
from tests.conftest import DEFAULT_PASSWORD

pytestmark = pytest.mark.security


@pytest.mark.parametrize("payload", SQL_INJECTION_STRINGS)
def test_sql_injection_in_portfolio_name_is_inert(client, user, db_session, payload):
    """Stored as text, never executed, and the schema survives."""
    response = client.post(
        "/portfolio",
        json={
            "name": payload,
            "asset_type": "gold",
            "amount": "50000",
            "horizon_years": 3,
        },
        headers=user["headers"],
    )
    assert response.status_code == 201
    assert response.json()["name"] == payload

    # Tables still exist and the user row was not touched.
    assert db_session.execute(select(func.count()).select_from(User)).scalar_one() >= 1
    stored = db_session.execute(
        select(Portfolio.name).where(Portfolio.id == response.json()["id"])
    ).scalar_one()
    assert stored == payload


@pytest.mark.parametrize("payload", SQL_INJECTION_STRINGS)
def test_sql_injection_in_login_does_not_authenticate(client, user, payload):
    response = client.post(
        "/auth/login", json={"email": payload, "password": payload}
    )
    # Either the address fails validation (422) or the credentials fail (401).
    # What must not happen is a 200.
    assert response.status_code in (401, 422), (
        f"payload {payload!r} produced {response.status_code}"
    )


def test_injection_payload_does_not_drop_a_table(client, user, db_session):
    client.post(
        "/portfolio",
        json={
            "name": "'; DROP TABLE portfolios; --",
            "asset_type": "gold",
            "amount": "50000",
            "horizon_years": 3,
        },
        headers=user["headers"],
    )
    # If the payload had executed, this query would raise.
    count = db_session.execute(text("SELECT COUNT(*) FROM portfolios")).scalar_one()
    assert count == 1


@pytest.mark.parametrize("payload", XSS_STRINGS)
def test_script_payloads_are_returned_as_data_not_html(client, user, payload):
    """The API is JSON-only, so a script payload is inert as long as it is
    served with a JSON content type and round-trips as an opaque string.

    Note the assertion this test does *not* make: JSON does not escape `<`,
    and does not need to. What keeps the payload inert is the content type,
    not the encoding of the body - which is why the nosniff test below
    matters.
    """
    response = client.post(
        "/portfolio",
        json={
            "name": payload,
            "asset_type": "hybrid",
            "amount": "50000",
            "horizon_years": 3,
        },
        headers=user["headers"],
    )
    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/json")
    assert "text/html" not in response.headers["content-type"]
    assert response.json()["name"] == payload


@pytest.mark.parametrize("endpoint", ["/health", "/portfolio"])
def test_responses_forbid_mime_sniffing(client, funded_user, endpoint):
    """BUG-002: without nosniff, a browser may re-interpret a JSON response
    holding a stored script payload as HTML and execute it."""
    response = client.get(endpoint, headers=funded_user["headers"])
    assert response.headers.get("x-content-type-options") == "nosniff"


def test_error_responses_do_not_leak_internals(client, user):
    """A validation error must not carry a traceback, file path or SQL."""
    response = client.post(
        "/portfolio",
        json={"name": "x", "asset_type": "nonsense", "amount": "abc", "horizon_years": "x"},
        headers=user["headers"],
    )
    assert response.status_code == 422
    body = response.text.lower()
    for leak in ["traceback", "sqlalchemy", "site-packages", ".py\"", "select ", "c:\\"]:
        assert leak not in body, f"error response leaked {leak!r}"


def test_404_does_not_confirm_existence_of_another_users_row(client, user, other_user):
    created = client.post(
        "/portfolio",
        json={
            "name": "Confidential",
            "asset_type": "equity_index",
            "amount": "50000",
            "horizon_years": 5,
        },
        headers=user["headers"],
    ).json()

    theirs = client.get(f"/portfolio/{created['id']}", headers=other_user["headers"])
    missing = client.get("/portfolio/999999", headers=other_user["headers"])

    # Indistinguishable: same status, same body.
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()
    assert "Confidential" not in theirs.text


def test_password_never_appears_in_any_response(client):
    response = client.post(
        "/users",
        json={
            "email": "leak@example.com",
            "full_name": "Leak Check",
            "password": DEFAULT_PASSWORD,
            "risk_score": 5,
        },
    )
    assert DEFAULT_PASSWORD not in response.text
    assert "pbkdf2" not in response.text


def test_cannot_set_balance_at_registration(client, db_session):
    """Mass assignment: an extra field must not seed an account with money."""
    response = client.post(
        "/users",
        json={
            "email": "rich@example.com",
            "full_name": "Opportunist",
            "password": DEFAULT_PASSWORD,
            "risk_score": 5,
            "balance": "1000000.00",
        },
    )
    assert response.status_code == 422


def test_cannot_assign_a_portfolio_to_another_user(client, user, other_user, db_session):
    """A user_id in the body must not override the authenticated caller."""
    response = client.post(
        "/portfolio",
        json={
            "name": "Planted",
            "asset_type": "gold",
            "amount": "50000",
            "horizon_years": 3,
            "user_id": other_user["account"]["id"],
        },
        headers=user["headers"],
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "endpoint,method",
    [
        ("/portfolio", "get"),
        ("/transactions", "get"),
        ("/recommendation", "post"),
        ("/chat", "post"),
    ],
)
def test_every_protected_endpoint_rejects_anonymous_access(client, endpoint, method):
    call = getattr(client, method)
    response = call(endpoint) if method == "get" else call(endpoint, json={})
    assert response.status_code == 401, f"{method.upper()} {endpoint} allowed anonymous access"


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("/users", {"email": "perf@example.com", "full_name": "P", "password": DEFAULT_PASSWORD, "risk_score": 5}),
    ],
)
def test_registration_stays_within_its_time_budget(client, endpoint, payload):
    """Registration is deliberately slow - 200k PBKDF2 rounds - but must stay
    inside 1.5s so the hashing cost cannot be turned into a DoS lever."""
    start = time.perf_counter()
    response = client.post(endpoint, json=payload)
    elapsed = time.perf_counter() - start
    assert response.status_code == 201
    assert elapsed < 1.5, f"registration took {elapsed:.3f}s"


def test_read_endpoints_respond_under_500ms(client, funded_user):
    for endpoint in ("/health", "/portfolio", "/transactions"):
        start = time.perf_counter()
        response = client.get(endpoint, headers=funded_user["headers"])
        elapsed = time.perf_counter() - start
        assert response.status_code == 200
        assert elapsed < 0.5, f"GET {endpoint} took {elapsed:.3f}s"


def test_oversized_payload_is_rejected_not_stored(client, user, db_session):
    """A 10k-character name must not reach the database."""
    response = client.post(
        "/portfolio",
        json={
            "name": "A" * 10_000,
            "asset_type": "gold",
            "amount": "50000",
            "horizon_years": 3,
        },
        headers=user["headers"],
    )
    assert response.status_code == 422
    assert db_session.execute(select(func.count()).select_from(Portfolio)).scalar_one() == 0


def test_balance_cannot_go_negative_through_any_sequence(client, funded_user, db_session):
    """Hammer the withdrawal path and assert the invariant afterwards."""
    for amount in ["50000.00", "0.01", "1000.00", "49999.99"]:
        client.post(
            "/transactions",
            json={"type": "withdrawal", "amount": amount},
            headers=funded_user["headers"],
        )
    balance = db_session.execute(
        select(User.balance).where(User.id == funded_user["account"]["id"])
    ).scalar_one()
    assert Decimal(balance) >= 0, f"balance went negative: {balance}"
