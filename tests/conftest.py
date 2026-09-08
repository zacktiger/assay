"""Test harness.

Each test gets a fresh in-memory database. StaticPool keeps every connection
pointed at the same in-memory instance - without it, SQLAlchemy hands the
TestClient thread a second, empty database and every insert vanishes.
"""
from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import llm
from app.database import Base, get_db
from app.llm.stub import ScriptedProvider, StubProvider
from app.routers.auth import reset_login_throttle
from app.main import app


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Iterator[Session]:
    """A session the test itself can use to inspect rows the API wrote."""
    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_engine) -> Iterator[TestClient]:
    factory = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    llm.set_provider(StubProvider())
    # The login throttle is module-level state that outlives a test's database,
    # so a lockout in one test would leak into the next.
    reset_login_throttle()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    llm.set_provider(None)


# --- account helpers ------------------------------------------------------

DEFAULT_PASSWORD = "correct-horse-9"


def register(client: TestClient, email: str, *, risk_score: int = 5, **kwargs) -> dict:
    payload = {
        "email": email,
        "full_name": kwargs.get("full_name", "Test User"),
        "password": kwargs.get("password", DEFAULT_PASSWORD),
        "risk_score": risk_score,
    }
    response = client.post("/users", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def login(client: TestClient, email: str, password: str = DEFAULT_PASSWORD) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user(client: TestClient) -> dict:
    """A registered, logged-in user with an authorization header ready."""
    account = register(client, "primary@example.com", risk_score=7)
    token = login(client, "primary@example.com")
    return {"account": account, "token": token, "headers": auth_header(token)}


@pytest.fixture
def other_user(client: TestClient) -> dict:
    """A second account, for cross-tenant access checks."""
    account = register(client, "intruder@example.com", risk_score=3)
    token = login(client, "intruder@example.com")
    return {"account": account, "token": token, "headers": auth_header(token)}


@pytest.fixture
def funded_user(client: TestClient, user: dict) -> dict:
    """The primary user with a known opening balance of 50,000."""
    response = client.post(
        "/transactions",
        json={"type": "deposit", "amount": "50000.00"},
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text
    assert Decimal(response.json()["balance_after"]) == Decimal("50000.00")
    return user


@pytest.fixture
def scripted_llm(client: TestClient) -> Iterator[ScriptedProvider]:
    """Feed the endpoint chosen model output, to exercise the guardrail layer.

    Depends on `client` so it is set up after that fixture installs the stub -
    otherwise the stub would overwrite the scripted provider.
    """
    provider = ScriptedProvider()
    llm.set_provider(provider)
    yield provider
    llm.set_provider(StubProvider())
