"""Create the schema. Run once as a release step, before the API starts.

    python -m scripts.init_db

This exists because creating tables from the application's own startup hook is
a race as soon as there is more than one worker: N processes issue CREATE TABLE
against the same database at the same time. Doing it here makes schema creation
a single, ordered step that either succeeds or fails the deploy.

It is create-only, not a migration tool. It will add tables that do not exist;
it will not alter a table whose columns have changed. A schema change against a
database holding real data needs Alembic - see docs/DEPLOY.md.
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import inspect, text

from app.config import settings
from app.database import Base, engine

# Importing the models module is what registers the tables on Base.metadata.
# Without it this script would create nothing and report success.
from app import models  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("init_db")


def main() -> int:
    safe_url = settings.database_url
    if "@" in safe_url:  # never log credentials
        safe_url = safe_url.split("@", 1)[1]
    logger.info("target database: %s", safe_url)

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("cannot reach the database: %s", exc)
        return 1

    before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(bind=engine)
    after = set(inspect(engine).get_table_names())

    created = sorted(after - before)
    if created:
        logger.info("created %d table(s): %s", len(created), ", ".join(created))
    else:
        logger.info("schema already present, nothing to create")

    expected = set(Base.metadata.tables)
    missing = sorted(expected - after)
    if missing:
        logger.error("expected tables still missing after create_all: %s", missing)
        return 1

    logger.info("schema ready (%d tables)", len(expected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
