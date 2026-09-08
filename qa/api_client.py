"""A thin client for driving the API over real HTTP.

The pytest suite runs in-process against TestClient, which is fast and gives
direct database access for the integrity checks. This client exists for the
other case: pointing the same calls at a deployed instance - a staging URL, a
container, a machine running `uvicorn app.main:app` - where in-process
shortcuts are not available.

    from qa.api_client import ApiClient

    api = ApiClient("http://localhost:8000")
    api.register("qa@example.com", password="correct-horse-9", risk_score=7)
    api.login("qa@example.com", "correct-horse-9")
    print(api.recommendation(amount="50000", horizon_years=5).json())
"""
from __future__ import annotations

from typing import Any

import httpx


class ApiClient:
    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self._token: str | None = None

    # --- plumbing ---------------------------------------------------------

    @property
    def token(self) -> str | None:
        return self._token

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        """Every call goes through here, so a test can send a deliberately
        unauthenticated request with `authenticated=False`."""
        sent = self._headers(headers) if authenticated else dict(headers or {})
        return self._client.request(method, path, json=json, headers=sent)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- endpoints --------------------------------------------------------

    def health(self) -> httpx.Response:
        return self.request("GET", "/health", authenticated=False)

    def register(
        self,
        email: str,
        *,
        password: str,
        full_name: str = "QA User",
        risk_score: int = 5,
    ) -> httpx.Response:
        return self.request(
            "POST",
            "/users",
            json={
                "email": email,
                "full_name": full_name,
                "password": password,
                "risk_score": risk_score,
            },
            authenticated=False,
        )

    def login(self, email: str, password: str) -> httpx.Response:
        response = self.request(
            "POST",
            "/auth/login",
            json={"email": email, "password": password},
            authenticated=False,
        )
        if response.status_code == 200:
            self._token = response.json()["access_token"]
        return response

    def get_user(self, user_id: int) -> httpx.Response:
        return self.request("GET", f"/users/{user_id}")

    def create_portfolio(
        self, *, name: str, asset_type: str, amount: str, horizon_years: int
    ) -> httpx.Response:
        return self.request(
            "POST",
            "/portfolio",
            json={
                "name": name,
                "asset_type": asset_type,
                "amount": amount,
                "horizon_years": horizon_years,
            },
        )

    def list_portfolios(self) -> httpx.Response:
        return self.request("GET", "/portfolio")

    def get_portfolio(self, portfolio_id: int) -> httpx.Response:
        return self.request("GET", f"/portfolio/{portfolio_id}")

    def transact(
        self, *, type: str, amount: str, idempotency_key: str | None = None
    ) -> httpx.Response:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self.request(
            "POST", "/transactions", json={"type": type, "amount": amount}, headers=headers
        )

    def list_transactions(self) -> httpx.Response:
        return self.request("GET", "/transactions")

    def recommendation(
        self, *, amount: str, horizon_years: int, asset_type: str | None = None
    ) -> httpx.Response:
        payload: dict[str, Any] = {"amount": amount, "horizon_years": horizon_years}
        if asset_type is not None:
            payload["asset_type"] = asset_type
        return self.request("POST", "/recommendation", json=payload)

    def chat(self, message: str) -> httpx.Response:
        return self.request("POST", "/chat", json={"message": message})
