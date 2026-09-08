"""User registration and retrieval."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import User
from app.security import verify_password
from qa.test_data import INVALID_EMAILS
from tests.conftest import DEFAULT_PASSWORD, auth_header, login, register


@pytest.mark.functional
def test_register_returns_201_and_the_created_user(client):
    response = client.post(
        "/users",
        json={
            "email": "new@example.com",
            "full_name": "New User",
            "password": DEFAULT_PASSWORD,
            "risk_score": 6,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new@example.com"
    assert body["risk_score"] == 6
    assert body["balance"] == "0.00"
    assert isinstance(body["id"], int)


@pytest.mark.security
def test_password_is_never_returned(client):
    body = register(client, "secret@example.com")
    assert "password" not in body
    assert "password_hash" not in body
    assert DEFAULT_PASSWORD not in str(body)


@pytest.mark.security
def test_password_is_stored_hashed_and_salted(client, db_session):
    register(client, "hashed@example.com")
    register(client, "hashed2@example.com", password=DEFAULT_PASSWORD)

    users = list(db_session.execute(select(User)).scalars())
    for stored in users:
        assert DEFAULT_PASSWORD not in stored.password_hash
        assert stored.password_hash.startswith("pbkdf2_sha256$")
        assert verify_password(DEFAULT_PASSWORD, stored.password_hash)

    # Same password, different hash - proves a per-user salt is in play.
    assert users[0].password_hash != users[1].password_hash


@pytest.mark.functional
def test_duplicate_email_returns_409(client):
    register(client, "dupe@example.com")
    response = client.post(
        "/users",
        json={
            "email": "dupe@example.com",
            "full_name": "Impostor",
            "password": DEFAULT_PASSWORD,
            "risk_score": 4,
        },
    )
    assert response.status_code == 409
    assert "already registered" in response.json()["detail"].lower()


@pytest.mark.functional
def test_duplicate_email_is_case_insensitive(client):
    """Addresses are normalised, so Dupe@ and dupe@ are one account.

    Without this, an attacker registers the same address in a different case
    and ends up with a second account that looks like the first one.
    """
    register(client, "case@example.com")
    response = client.post(
        "/users",
        json={
            "email": "CASE@example.com",
            "full_name": "Impostor",
            "password": DEFAULT_PASSWORD,
            "risk_score": 4,
        },
    )
    assert response.status_code == 409


@pytest.mark.negative
@pytest.mark.parametrize(
    "email,label", INVALID_EMAILS, ids=[label for _, label in INVALID_EMAILS]
)
def test_invalid_email_rejected(client, email, label):
    response = client.post(
        "/users",
        json={
            "email": email,
            "full_name": "Test",
            "password": DEFAULT_PASSWORD,
            "risk_score": 5,
        },
    )
    assert response.status_code == 422, f"{label}: {email!r} was accepted"


@pytest.mark.negative
@pytest.mark.parametrize(
    "password,label",
    [("", "empty"), ("short", "5 chars"), ("1234567", "7 chars, one below minimum")],
)
def test_short_password_rejected(client, password, label):
    response = client.post(
        "/users",
        json={
            "email": "pw@example.com",
            "full_name": "Test",
            "password": password,
            "risk_score": 5,
        },
    )
    assert response.status_code == 422, f"{label} password accepted"


@pytest.mark.negative
@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_blank_full_name_rejected(client, name):
    response = client.post(
        "/users",
        json={
            "email": "blank@example.com",
            "full_name": name,
            "password": DEFAULT_PASSWORD,
            "risk_score": 5,
        },
    )
    assert response.status_code == 422


@pytest.mark.functional
def test_get_own_user(client, user):
    response = client.get(f"/users/{user['account']['id']}", headers=user["headers"])
    assert response.status_code == 200
    assert response.json()["email"] == "primary@example.com"


@pytest.mark.security
def test_cannot_read_another_users_record(client, user, other_user):
    """Cross-tenant read must not succeed, and must not confirm the id exists."""
    victim_id = user["account"]["id"]
    response = client.get(f"/users/{victim_id}", headers=other_user["headers"])
    assert response.status_code == 404
    assert "primary@example.com" not in response.text


@pytest.mark.security
def test_get_user_requires_authentication(client, user):
    response = client.get(f"/users/{user['account']['id']}")
    assert response.status_code == 401


@pytest.mark.negative
def test_nonexistent_user_returns_404(client, user):
    response = client.get("/users/999999", headers=user["headers"])
    assert response.status_code == 404


@pytest.mark.negative
def test_non_integer_user_id_returns_422(client, user):
    response = client.get("/users/not-an-id", headers=user["headers"])
    assert response.status_code == 422


@pytest.mark.functional
def test_registration_is_case_normalised_for_login(client):
    """Registering as Mixed@ must allow logging in as mixed@."""
    client.post(
        "/users",
        json={
            "email": "Mixed@Example.com",
            "full_name": "Mixed Case",
            "password": DEFAULT_PASSWORD,
            "risk_score": 5,
        },
    )
    token = login(client, "mixed@example.com")
    response = client.get("/portfolio", headers=auth_header(token))
    assert response.status_code == 200
