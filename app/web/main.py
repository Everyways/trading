"""FastAPI application — trading bot web dashboard.

Usage:
    python scripts/run_dashboard.py
    uvicorn app.web.main:app --host 0.0.0.0 --port 8080

Protected by HTTP Basic Auth (DASHBOARD_USER / DASHBOARD_PASSWORD in .env).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.web.routes import api, dashboard

app = FastAPI(
    title="Trading Bot Dashboard",
    docs_url=None,   # disable Swagger (no public API docs)
    redoc_url=None,
)

app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)

app.include_router(dashboard.router)
app.include_router(api.router, prefix="/api")
