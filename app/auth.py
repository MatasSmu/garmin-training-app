"""HTTP Basic Auth middleware -- gates every request, API and static alike.

This is the only thing standing between the LAN and this app's personal
health data once it's bound to 0.0.0.0, so it runs as middleware (covering
static files too) rather than a per-route dependency that's easy to forget
to add to a new router.
"""

from __future__ import annotations

import base64
import binascii
import secrets

from starlette.requests import Request
from starlette.responses import Response

from app.config import APP_PASSWORD, APP_USERNAME

UNAUTHORIZED = Response(
    status_code=401,
    headers={"WWW-Authenticate": 'Basic realm="Training"'},
)


def _credentials_valid(header_value: str | None) -> bool:
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value[len("Basic ") :]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(username, APP_USERNAME) and secrets.compare_digest(
        password, APP_PASSWORD
    )


async def require_auth(request: Request, call_next):
    if not _credentials_valid(request.headers.get("Authorization")):
        return UNAUTHORIZED
    return await call_next(request)
