"""Database initialization script.

Creates all tables from SQLModel metadata. Use this instead of Alembic
for local development and paper trading with SQLite.

For PostgreSQL production: use `alembic upgrade head` instead.

Usage:
    python scripts/init_db.py                    # uses DATABASE_URL_SYNC from .env
    python scripts/init_db.py --url sqlite:///./data/paper.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")
log = logging.getLogger("init_db")


def main() -> None:
    p = argparse.ArgumentParser(description="Initialise the trading bot database")
    p.add_argument(
        "--url",
        metavar="DSN",
        help="SQLAlchemy sync DSN (overrides DATABASE_URL_SYNC from .env)",
    )
    args = p.parse_args()

    if args.url:
        # Override before importing app code that reads settings
        import os
        os.environ["DATABASE_URL_SYNC"] = args.url
        os.environ.setdefault("DATABASE_URL", args.url)
        # Provide dummy values for required-but-unused settings
        os.environ.setdefault("SECRET_KEY", "local-dev-secret-key-not-used-in-db-init")
        os.environ.setdefault("DASHBOARD_PASSWORD", "dummy")

    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel

    # Import all models to register them with SQLModel metadata
    import app.data.models  # noqa: F401

    # Determine URL
    if args.url:
        url = args.url
    else:
        from app.config import get_settings
        url = get_settings().database_url_sync

    log.info("Connecting to: %s", url)

    sqlite = url.startswith("sqlite")
    engine_kwargs: dict = {}
    if sqlite:
        engine_kwargs["connect_args"] = {"check_same_thread": False}
        engine_kwargs["poolclass"] = StaticPool
        # Create parent directory if needed
        if ":///" in url and not url.endswith(":memory:"):
            db_path = Path(url.split("///", 1)[1])
            db_path.parent.mkdir(parents=True, exist_ok=True)
            log.info("Database file: %s", db_path.resolve())

    engine = create_engine(url, **engine_kwargs)

    # Check existing tables
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    if existing:
        log.info("Existing tables: %s", sorted(existing))

    SQLModel.metadata.create_all(engine)

    # Verify
    inspector = inspect(engine)
    created = set(inspector.get_table_names())
    new_tables = created - existing
    if new_tables:
        log.info("Created tables: %s", sorted(new_tables))
    else:
        log.info("All tables already exist — no changes made")

    # TimescaleDB hypertable (PostgreSQL only, no-op on SQLite)
    if not sqlite:
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "SELECT create_hypertable('ohlcv', 'time', "
                    "if_not_exists => TRUE, migrate_data => TRUE)"
                ))
                conn.commit()
            log.info("TimescaleDB hypertable configured for ohlcv")
        except Exception:
            log.debug("TimescaleDB not available — using plain PostgreSQL")

    engine.dispose()
    log.info("Database initialised successfully")


if __name__ == "__main__":
    main()
