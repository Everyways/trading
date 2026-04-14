"""Database backup script.

Creates a timestamped backup of the trading database.

Usage:
    python scripts/backup_db.py [--output-dir ./backups]

SQLite  → Python sqlite3.backup() into <output_dir>/trading_<YYYYMMDD_HHMMSS>.db
PostgreSQL → pg_dump into <output_dir>/trading_<YYYYMMDD_HHMMSS>.dump
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backup_db")


def _timestamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")


def backup_sqlite(db_path: str, output_dir: Path) -> Path:
    """Copy a SQLite database using the built-in backup API (safe under concurrent writes)."""
    import sqlite3

    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"SQLite DB not found: {src}")

    dest = output_dir / f"trading_{_timestamp()}.db"
    with sqlite3.connect(str(src)) as src_conn, sqlite3.connect(str(dest)) as dst_conn:
        src_conn.backup(dst_conn)

    log.info("SQLite backup: %s → %s (%.1f KB)", src, dest, dest.stat().st_size / 1024)
    return dest


def backup_postgres(dsn: str, output_dir: Path) -> Path:
    """Dump a PostgreSQL database with pg_dump."""
    if shutil.which("pg_dump") is None:
        raise RuntimeError("pg_dump not found in PATH — install postgresql-client")

    dest = output_dir / f"trading_{_timestamp()}.dump"
    cmd = ["pg_dump", "--format=custom", f"--file={dest}", dsn]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed:\n{result.stderr}")

    log.info("PostgreSQL backup: → %s (%.1f KB)", dest, dest.stat().st_size / 1024)
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the trading database")
    parser.add_argument(
        "--output-dir",
        default="./backups",
        help="Directory to write the backup file (default: ./backups)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load settings (may fail if .env is missing — that is intentional)
    try:
        from app.config import get_settings
        dsn = get_settings().database_url_sync
    except Exception as exc:
        log.error("Could not load settings: %s", exc)
        sys.exit(1)

    parsed = urlparse(dsn)
    scheme = parsed.scheme.split("+")[0]  # e.g. "sqlite", "postgresql", "postgres"

    try:
        if scheme == "sqlite":
            # DSN format: sqlite:///./path/to/db.sqlite3
            db_path = parsed.path.lstrip("/")
            backup_sqlite(db_path, output_dir)
        elif scheme in ("postgresql", "postgres"):
            backup_postgres(dsn, output_dir)
        else:
            log.error("Unsupported DB scheme: %s", scheme)
            sys.exit(1)
    except Exception as exc:
        log.error("Backup failed: %s", exc)
        sys.exit(1)

    log.info("Backup complete.")


if __name__ == "__main__":
    main()
