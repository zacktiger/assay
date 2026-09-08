"""Runtime configuration, read from the environment once at import time.

Import-time reads are deliberate: a container gets its environment before the
process starts, so there is nothing to reload later, and a frozen settings
object cannot drift mid-request.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from dotenv import load_dotenv

# Load a local .env if present. Real environment variables always win, so this
# is a developer convenience and a no-op in a container.
load_dotenv(override=False)


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


ENVIRONMENT = os.getenv("ENVIRONMENT", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"


@dataclass(frozen=True)
class Settings:
    environment: str = ENVIRONMENT
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./finance.db")
    llm_provider: str = os.getenv("LLM_PROVIDER", "stub")
    llm_model: str = os.getenv("LLM_MODEL", "claude-opus-5")
    token_ttl_seconds: int = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))

    # Schema creation on startup is convenient in development and unsafe with
    # more than one worker, so production must opt in explicitly - or better,
    # run `python -m scripts.init_db` once as a release step.
    auto_create_tables: bool = _flag("AUTO_CREATE_TABLES", not IS_PRODUCTION)

    # The OpenAPI schema documents every field and bound. Off by default in
    # production; set ENABLE_DOCS=true to expose it deliberately.
    enable_docs: bool = _flag("ENABLE_DOCS", not IS_PRODUCTION)

    # No origins by default: a browser client must be named explicitly. An
    # empty list disables CORS entirely, which is correct for a server-to-
    # server API.
    cors_origins: list[str] = field(default_factory=lambda: _csv("CORS_ORIGINS"))

    # Failed-login throttle. In-process only - see the note in routers/auth.py.
    login_max_attempts: int = int(os.getenv("LOGIN_MAX_ATTEMPTS", "10"))
    login_window_seconds: int = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))

    # Hard ceiling on any list endpoint, so a caller cannot ask for the whole
    # table in one request.
    max_page_size: int = int(os.getenv("MAX_PAGE_SIZE", "100"))
    default_page_size: int = int(os.getenv("DEFAULT_PAGE_SIZE", "50"))

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()

# --- Business rules -------------------------------------------------------
# These bounds are the contract the QA suite pins down. They are deliberately
# defined in one place so a boundary test and the validator cannot drift apart.
MIN_INVESTMENT = Decimal("1000")
MAX_INVESTMENT = Decimal("100000")
MIN_HORIZON_YEARS = 1
MAX_HORIZON_YEARS = 40
MIN_RISK_SCORE = 1
MAX_RISK_SCORE = 10

ASSET_TYPES = ("equity_index", "debt_fund", "gold", "fixed_deposit", "hybrid")

# Expected nominal annual return per asset type, in percent. Deterministic:
# the projection maths never goes near the LLM.
EXPECTED_RETURN_PCT = {
    "equity_index": Decimal("12.0"),
    "debt_fund": Decimal("7.0"),
    "gold": Decimal("8.0"),
    "fixed_deposit": Decimal("6.5"),
    "hybrid": Decimal("9.5"),
}

# Risk appetite each asset type demands, on the same 1-10 scale as the user.
ASSET_RISK_LEVEL = {
    "fixed_deposit": 1,
    "debt_fund": 3,
    "gold": 5,
    "hybrid": 6,
    "equity_index": 8,
}
