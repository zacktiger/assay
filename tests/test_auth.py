"""Authentication: login, token issuance, token lifetime."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import AuthToken
from tests.conftest import DEFAULT_PASSWORD, auth_header, login, register

pytestmark = pytest.mark.security


@pytest.mark.functional
def test_login_returns_a_bearer_token(client):
    register(client, "login@example.com")
    response = client.post(
        "/auth/login", json={"email": "login@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert isinstance(body["access_token"], str)
    assert len(body["access_token"]) >= 32
    assert body["expires_in"] > 0


def test_wrong_password_returns_401(client):
    register(client, "wrong@example.com")
    response = client.post(
        "/auth/login", json={"email": "wrong@example.com", "password": "not-the-password"}
    )
    assert response.status_code == 401


def test_unknown_email_returns_401_not_404(client):
    """A 404 here would confirm which addresses have accounts."""
    response = client.post(
        "/auth/login", json={"email": "ghost@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 401


def test_error_message_does_not_distinguish_unknown_user_from_bad_password(client):
    register(client, "enum@example.com")
    unknown = client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": DEFAULT_PASSWORD}
    )
    bad_password = client.post(
        "/auth/login", json={"email": "enum@example.com", "password": "wrong-password"}
    )
    assert unknown.status_code == bad_password.status_code == 401
    assert unknown.json()["detail"] == bad_password.json()["detail"]


def test_raw_token_is_not_stored_in_the_database(client, db_session):
    """A database dump must not hand over live sessions."""
    register(client, "tok@example.com")
    token = login(client, "tok@example.com")

    stored = list(db_session.execute(select(AuthToken)).scalars())
    assert len(stored) == 1
    assert stored[0].token_hash != token
    assert token not in stored[0].token_hash


def test_each_login_issues_a_distinct_token(client):
    register(client, "multi@example.com")
    first = login(client, "multi@example.com")
    second = login(client, "multi@example.com")
    assert first != second


@pytest.mark.parametrize(
    "header,label",
    [
        ({}, "no header"),
        ({"Authorization": ""}, "empty header"),
        ({"Authorization": "Bearer "}, "bearer with no token"),
        ({"Authorization": "Bearer not-a-real-token"}, "made-up token"),
        ({"Authorization": "Basic dXNlcjpwYXNz"}, "wrong scheme"),
        ({"Authorization": "not-a-real-token"}, "no scheme"),
    ],
)
def test_bad_authorization_headers_are_rejected(client, header, label):
    response = client.get("/portfolio", headers=header)
    assert response.status_code in (401, 403), f"{label} was accepted"


def test_expired_token_is_rejected(client, db_session):
    """Backdate the stored expiry rather than waiting out the TTL."""
    register(client, "expiry@example.com")
    token = login(client, "expiry@example.com")

    assert client.get("/portfolio", headers=auth_header(token)).status_code == 200

    row = db_session.execute(select(AuthToken)).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    response = client.get("/portfolio", headers=auth_header(token))
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_token_from_one_account_cannot_read_another(client, user, other_user):
    victim_portfolio = client.post(
        "/portfolio",
        json={
            "name": "Private",
            "asset_type": "equity_index",
            "amount": "50000",
            "horizon_years": 5,
        },
        headers=user["headers"],
    ).json()

    response = client.get(
        f"/portfolio/{victim_portfolio['id']}", headers=other_user["headers"]
    )
    assert response.status_code == 404


def test_login_rejects_unknown_fields(client):
    register(client, "extra@example.com")
    response = client.post(
        "/auth/login",
        json={
            "email": "extra@example.com",
            "password": DEFAULT_PASSWORD,
            "role": "admin",
        },
    )
    assert response.status_code == 422
