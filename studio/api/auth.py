"""Single-user session auth + CSRF for Content Studio.

Wire model:

* ``POST /api/session`` validates the password against
  ``Settings.content_studio_password`` and, on success, mints a session
  cookie carrying the session id and a random CSRF token. The cookie is
  HMAC'd with ``Settings.content_studio_session_secret`` (random per-process
  when unset) so a cookie cannot be forged outside the running app.
* Every other route depends on :func:`require_session` (validates cookie
  HMAC) and :func:`require_csrf` (matches the request's ``X-CSRF-Token``
  header against the per-session token stored in the cookie).

The CSRF token is also returned in the login response body so the front-end
can attach it as ``X-CSRF-Token`` on every mutating request — the cookie
itself is ``HttpOnly`` and the SPA cannot read it directly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status

from studio.api.dependencies import get_settings
from studio.config import Settings

COOKIE_NAME = "content_studio_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_LIFETIME = timedelta(days=7)


@dataclass(frozen=True)
class SessionInfo:
    """Validated session payload returned by :func:`get_current_session`."""

    id: str
    csrf_token: str


_PROCESS_SECRET: bytes | None = None


def _secret_key(settings: Settings) -> bytes:
    """Resolve the HMAC key, generating a random one per process if absent."""

    global _PROCESS_SECRET
    raw = settings.content_studio_session_secret
    if raw:
        return raw.encode("utf-8")
    if _PROCESS_SECRET is None:
        _PROCESS_SECRET = hashlib.sha256(
            b"content-studio-session|" + secrets.token_bytes(32)
        ).digest()
    return _PROCESS_SECRET


def reset_session_secret() -> None:
    """Drop the cached process-local HMAC key (test-only)."""

    global _PROCESS_SECRET
    _PROCESS_SECRET = None


def _b64url_encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _b64url_decode(payload: str) -> bytes:
    padding = "=" * (-len(payload) % 4)
    return base64.urlsafe_b64decode(payload + padding)


def _sign(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _cookie_settings(settings: Settings) -> dict[str, Any]:
    return {
        "httponly": True,
        "secure": settings.production,
        "samesite": "strict",
        "path": "/",
    }


def mint_session(settings: Settings) -> tuple[str, str]:
    """Mint a fresh session payload and signed cookie value.

    Returns ``(cookie_value, csrf_token)``. The csrf token is also embedded
    in the cookie payload so the server can verify it later; the same value
    is returned to the client via the login response body.
    """

    session_id = secrets.token_urlsafe(24)
    csrf_token = secrets.token_hex(32)
    issued_at = datetime.now(UTC)
    payload = {
        "id": session_id,
        "csrf": csrf_token,
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + SESSION_LIFETIME).timestamp()),
    }
    encoded = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    secret = _secret_key(settings)
    signature = _sign(secret, encoded.encode("ascii"))
    return f"{encoded}.{signature}", csrf_token


def verify_cookie(settings: Settings, cookie_value: str) -> SessionInfo | None:
    """Return the session info iff the cookie is valid and unexpired."""

    if not cookie_value or "." not in cookie_value:
        return None
    encoded, _, signature = cookie_value.partition(".")
    secret = _secret_key(settings)
    if not hmac.compare_digest(_sign(secret, encoded.encode("ascii")), signature):
        return None
    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(datetime.now(UTC).timestamp()):
        return None
    session_id = payload.get("id")
    csrf_token = payload.get("csrf")
    if not isinstance(session_id, str) or not isinstance(csrf_token, str):
        return None
    return SessionInfo(id=session_id, csrf_token=csrf_token)


def set_session_cookie(response: Response, cookie_value: str, settings: Settings) -> None:
    response.set_cookie(COOKIE_NAME, cookie_value, **_cookie_settings(settings))


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


async def get_current_session(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> SessionInfo | None:
    """Read + verify the session cookie. Returns ``None`` when absent/invalid."""

    cookie_value = request.cookies.get(COOKIE_NAME)
    if not cookie_value:
        return None
    return verify_cookie(settings, cookie_value)


async def require_session(
    request: Request,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> SessionInfo:
    """Reject unauthenticated requests with 401."""

    cookie_value = request.cookies.get(COOKIE_NAME)
    info = verify_cookie(settings, cookie_value) if cookie_value else None
    if info is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session required")
    return info


async def require_csrf(
    request: Request,
    session: SessionInfo = Depends(require_session),  # noqa: B008
) -> SessionInfo:
    """Reject mutating requests whose CSRF header doesn't match the cookie."""

    header = request.headers.get(CSRF_HEADER, "")
    if not header or not hmac.compare_digest(header, session.csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf token invalid")
    return session


def check_password(settings: Settings, supplied: str) -> bool:
    """Constant-time comparison of the supplied password vs Settings."""

    expected = settings.content_studio_password
    if not expected:
        # An empty configured password means "no auth possible" — every
        # attempt must fail closed. Returning ``False`` keeps the route's
        # public surface identical to a misconfiguration.
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


__all__ = [
    "COOKIE_NAME",
    "CSRF_HEADER",
    "SessionInfo",
    "check_password",
    "clear_session_cookie",
    "get_current_session",
    "mint_session",
    "require_csrf",
    "require_session",
    "set_session_cookie",
    "verify_cookie",
]