"""Deployment-surface tests.

These cover the parts that only matter once the app leaves a laptop: probes,
headers, paging limits, token revocation, the login throttle, and the
environment gating that decides whether the schema is auto-created and the
OpenAPI docs are public.

The environment-gating tests run a subprocess with a different environment,
because settings are frozen at import time - which is the behaviour under test,
not an obstacle to it.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from decimal import Decimal

import pytest

from app.config import settings
from tests.conftest import DEFAULT_PASSWORD, auth_header, login, register

pytestmark = pytest.mark.deployment


# --- probes ---------------------------------------------------------------


def test_liveness_probe_needs_no_auth_and_no_database(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_probe_reports_database_state(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "up"


# --- headers --------------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
    ],
)
def test_security_headers_present_on_every_response(client, header, expected):
    assert client.get("/health").headers.get(header) == expected


def test_request_id_is_returned(client):
    """A user-reported failure has to be findable in the logs."""
    assert client.get("/health").headers.get("x-request-id")


def test_supplied_request_id_is_echoed(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-me-123"})
    assert response.headers["x-request-id"] == "trace-me-123"


def test_error_responses_also_carry_security_headers(client, user):
    """A 4xx must not skip the middleware."""
    response = client.get("/portfolio/999999", headers=user["headers"])
    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"


# --- paging ---------------------------------------------------------------


def _make_portfolios(client, headers, count):
    for i in range(count):
        response = client.post(
            "/portfolio",
            json={
                "name": f"Holding {i:02d}",
                "asset_type": "gold",
                "amount": "50000",
                "horizon_years": 5,
            },
            headers=headers,
        )
        assert response.status_code == 201


def test_list_is_paged(client, user):
    _make_portfolios(client, user["headers"], 5)
    page = client.get("/portfolio?limit=2", headers=user["headers"]).json()
    assert len(page) == 2
    assert [p["name"] for p in page] == ["Holding 00", "Holding 01"]


def test_offset_walks_the_list_without_gaps_or_repeats(client, user):
    _make_portfolios(client, user["headers"], 5)
    seen = []
    for offset in range(0, 6, 2):
        page = client.get(
            f"/portfolio?limit=2&offset={offset}", headers=user["headers"]
        ).json()
        seen.extend(p["name"] for p in page)
    assert seen == [f"Holding {i:02d}" for i in range(5)]


def test_limit_above_the_cap_is_rejected(client, user):
    """A caller must not be able to ask for the whole table."""
    over = settings.max_page_size + 1
    response = client.get(f"/portfolio?limit={over}", headers=user["headers"])
    assert response.status_code == 422


@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_invalid_paging_parameters_rejected(client, user, bad):
    assert client.get(f"/portfolio?limit={bad}", headers=user["headers"]).status_code == 422


def test_negative_offset_rejected(client, user):
    assert client.get("/portfolio?offset=-1", headers=user["headers"]).status_code == 422


def test_transactions_are_paged_too(client, user):
    for _ in range(4):
        client.post(
            "/transactions",
            json={"type": "deposit", "amount": "1000.00"},
            headers=user["headers"],
        )
    page = client.get("/transactions?limit=3", headers=user["headers"]).json()
    assert len(page) == 3
    # Oldest first, so a ledger reads forwards and offsets stay stable.
    assert Decimal(page[0]["balance_after"]) == Decimal("1000.00")


def test_default_paging_applies_without_parameters(client, user):
    _make_portfolios(client, user["headers"], 3)
    assert len(client.get("/portfolio", headers=user["headers"]).json()) == 3


# --- token revocation -----------------------------------------------------


def test_logout_revokes_the_token(client):
    register(client, "logout@example.com")
    token = login(client, "logout@example.com")
    headers = auth_header(token)

    assert client.get("/portfolio", headers=headers).status_code == 200
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/portfolio", headers=headers).status_code == 401


def test_logout_does_not_revoke_other_sessions(client):
    """Signing out of one device must not sign the user out everywhere."""
    register(client, "twodevice@example.com")
    phone = auth_header(login(client, "twodevice@example.com"))
    laptop = auth_header(login(client, "twodevice@example.com"))

    assert client.post("/auth/logout", headers=phone).status_code == 204
    assert client.get("/portfolio", headers=phone).status_code == 401
    assert client.get("/portfolio", headers=laptop).status_code == 200


def test_logout_requires_authentication(client):
    assert client.post("/auth/logout").status_code == 401


def test_expired_tokens_are_swept_on_login(client, db_session):
    """The auth_tokens table must not grow without bound."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from app.models import AuthToken

    register(client, "sweep@example.com")
    login(client, "sweep@example.com")

    row = db_session.execute(select(AuthToken)).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.commit()

    login(client, "sweep@example.com")

    remaining = db_session.execute(select(func.count()).select_from(AuthToken)).scalar_one()
    assert remaining == 1, "the expired token was not swept"


# --- login throttle -------------------------------------------------------


def test_repeated_failures_lock_the_account_out(client):
    register(client, "brute@example.com")

    for _ in range(settings.login_max_attempts):
        response = client.post(
            "/auth/login", json={"email": "brute@example.com", "password": "wrong"}
        )
        assert response.status_code == 401

    blocked = client.post(
        "/auth/login", json={"email": "brute@example.com", "password": "wrong"}
    )
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after")


def test_lockout_also_blocks_the_correct_password(client):
    """Otherwise the throttle is trivially bypassed by the attacker who wins."""
    register(client, "locked@example.com")
    for _ in range(settings.login_max_attempts):
        client.post("/auth/login", json={"email": "locked@example.com", "password": "wrong"})

    response = client.post(
        "/auth/login", json={"email": "locked@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 429


def test_throttle_is_scoped_per_account(client):
    """One account being attacked must not lock out everyone else."""
    register(client, "victim@example.com")
    register(client, "bystander@example.com")

    for _ in range(settings.login_max_attempts):
        client.post("/auth/login", json={"email": "victim@example.com", "password": "wrong"})

    assert client.post(
        "/auth/login",
        json={"email": "bystander@example.com", "password": DEFAULT_PASSWORD},
    ).status_code == 200


def test_a_successful_login_clears_the_failure_count(client):
    register(client, "recover@example.com")
    for _ in range(settings.login_max_attempts - 1):
        client.post("/auth/login", json={"email": "recover@example.com", "password": "wrong"})

    assert login(client, "recover@example.com")

    # Counter reset, so the next wrong password is failure #1, not #10.
    response = client.post(
        "/auth/login", json={"email": "recover@example.com", "password": "wrong"}
    )
    assert response.status_code == 401


# --- environment gating ---------------------------------------------------


def _config_in_env(**env: str) -> dict:
    """Import app.config in a subprocess with a given environment."""
    code = (
        "import json;from app.config import settings;"
        "print(json.dumps({"
        "'environment': settings.environment,"
        "'auto_create_tables': settings.auto_create_tables,"
        "'enable_docs': settings.enable_docs,"
        "'cors_origins': settings.cors_origins,"
        "'is_production': settings.is_production}))"
    )
    import json
    import os

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_production_disables_auto_table_creation():
    """Creating tables from N workers at once is a race; production opts out."""
    config = _config_in_env(ENVIRONMENT="production")
    assert config["is_production"] is True
    assert config["auto_create_tables"] is False


def test_production_hides_the_openapi_docs():
    assert _config_in_env(ENVIRONMENT="production")["enable_docs"] is False


def test_development_keeps_docs_and_auto_create_on():
    config = _config_in_env(ENVIRONMENT="development")
    assert config["auto_create_tables"] is True
    assert config["enable_docs"] is True


def test_production_gates_can_be_overridden_explicitly():
    """An operator who wants docs in production must ask for them by name."""
    config = _config_in_env(ENVIRONMENT="production", ENABLE_DOCS="true")
    assert config["enable_docs"] is True


def test_cors_is_closed_by_default():
    assert _config_in_env(ENVIRONMENT="production")["cors_origins"] == []


def test_cors_origins_parse_from_a_comma_separated_list():
    config = _config_in_env(
        CORS_ORIGINS="https://app.example.com, https://staging.example.com"
    )
    assert config["cors_origins"] == [
        "https://app.example.com",
        "https://staging.example.com",
    ]


def test_docs_are_absent_when_disabled():
    """Gating the URLs must actually remove the routes, not just hide links."""
    code = (
        "from fastapi.testclient import TestClient;"
        "from app.main import app;"
        "c=TestClient(app);"
        "print(c.get('/openapi.json').status_code, c.get('/docs').status_code)"
    )
    import os

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "ENVIRONMENT": "production", "AUTO_CREATE_TABLES": "false"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "404 404"


# --- schema bootstrap -----------------------------------------------------


def test_init_db_creates_the_schema_and_is_idempotent(tmp_path):
    """The release step must be safe to run on every deploy."""
    import os

    db_path = tmp_path / "deploy.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path.as_posix()}"}

    first = subprocess.run(
        [sys.executable, "-m", "scripts.init_db"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert first.returncode == 0, first.stderr
    assert "created 5 table(s)" in first.stderr

    second = subprocess.run(
        [sys.executable, "-m", "scripts.init_db"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert second.returncode == 0, second.stderr
    assert "nothing to create" in second.stderr


def test_init_db_fails_loudly_on_an_unreachable_database():
    """A deploy must stop here rather than starting a broken API."""
    import os

    result = subprocess.run(
        [sys.executable, "-m", "scripts.init_db"],
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": "sqlite:////no-such-dir-xyz/a/b.db"},
        timeout=120,
    )
    assert result.returncode == 1
    assert "cannot reach the database" in result.stderr


def test_missing_driver_gives_an_actionable_message():
    """A bad DATABASE_URL should name the missing package, not dump a stack."""
    import os

    result = subprocess.run(
        [sys.executable, "-c", "import app.database"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": "postgresql+psycopg://u:p@host:5432/db",
        },
        timeout=60,
    )
    assert result.returncode != 0
    assert "psycopg" in result.stderr
    assert "pip install" in result.stderr


# --- logging --------------------------------------------------------------


def test_app_logger_is_configured_to_emit():
    """The regression: the "app" logger had no handler and inherited the root
    level of WARNING, so every logger.info was discarded - request ids went out
    in response headers with no log line to match them to."""
    app_logger = logging.getLogger("app")
    assert app_logger.handlers, "app logger has no handler; INFO records go nowhere"
    assert app_logger.isEnabledFor(logging.INFO)


def test_request_logging_records_method_status_and_request_id(client):
    """Capture on the logger itself - a stream handler bound at import time
    cannot be intercepted through captured stdout."""
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    app_logger = logging.getLogger("app")
    handler = Capture(level=logging.INFO)
    app_logger.addHandler(handler)
    try:
        assert client.get("/health", headers={"X-Request-ID": "log-check-77"}).status_code == 200
    finally:
        app_logger.removeHandler(handler)

    messages = [record.getMessage() for record in records]
    assert any("log-check-77" in message for message in messages), messages
    assert any("GET /health -> 200" in message for message in messages), messages
