"""Dashboard web server — entry point.

Usage:
    python scripts/run_dashboard.py                    # default: 0.0.0.0:8080
    python scripts/run_dashboard.py --port 8090
    python scripts/run_dashboard.py --host 127.0.0.1

Requires environment variables (or .env file):
    DATABASE_URL_SYNC      — SQLite or PostgreSQL sync DSN
    DASHBOARD_USER         — username  (default: admin)
    DASHBOARD_PASSWORD     — password  (required)

Access the dashboard at http://<host>:8080/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Trading bot dashboard server")
    p.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    p.add_argument("--reload", action="store_true", help="Enable auto-reload (dev only)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    import uvicorn

    uvicorn.run(
        "app.web.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
