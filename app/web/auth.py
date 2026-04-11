"""HTTP Basic Auth dependency for the dashboard."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings

_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    """FastAPI dependency — raises 401 if credentials don't match settings."""
    s = get_settings()
    ok = secrets.compare_digest(
        credentials.username.encode(), s.dashboard_user.encode()
    ) and secrets.compare_digest(
        credentials.password.encode(), s.dashboard_password.encode()
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
