"""Stable ``{code, message, details}`` error envelope for Content Studio.

Routes raise ``ApiError`` (or FastAPI's ``HTTPException``) and the handlers
registered by :func:`register_error_handlers` translate both into the same
JSON shape so the web UI can switch on ``code`` without parsing free-form
``detail`` strings.

The shape mirrors the contract pinned in Task 10:

* ``code`` — stable machine identifier (e.g. ``confirmation_required``).
* ``message`` — human-readable explanation.
* ``details`` — optional object with extra fields the client may need
  (currently only ``invalidates`` for the reopen handshake).

``HTTPException.detail`` is reused as the response ``message`` so that
existing call sites that raise ``HTTPException`` keep working without
re-writing every route.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    """Raise from a route to emit a structured error response.

    ``details`` is merged into the response body so the caller can surface
    things like ``invalidates`` (the list of downstream artifact kinds that
    a reopen will discard).
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


def _envelope(code: str, message: str, details: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    # Flatten ``details`` into the top level so callers can access fields like
    # ``invalidates`` without an extra ``details.`` hop. The shape stays
    # ``{code, message, ...}``; details-specific keys sit alongside.
    if details:
        for key, value in details.items():
            body[key] = value
    return body


async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(exc.code, exc.message, exc.details),
    )


async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException | HTTPException
) -> JSONResponse:
    """Map FastAPI / Starlette ``HTTPException`` to the envelope shape.

    If the original ``detail`` already looks like an envelope (``code`` +
    ``message``), pass it through untouched; otherwise synthesise a code
    from the status text and reuse ``detail`` as the message.
    """

    detail = exc.detail
    code: str
    message: str
    extra: dict[str, Any] = {}
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        code = str(detail["code"])
        message = str(detail["message"])
        for k, v in detail.items():
            if k not in {"code", "message"}:
                extra[k] = v
    else:
        code = _status_to_code(exc.status_code)
        message = str(detail) if detail is not None else ""
    # ``invalidates`` may be a top-level detail when callers want it visible
    # without an explicit ``details`` wrapper; surface it there too.
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, message, extra or None),
    )


def _status_to_code(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        422: "unprocessable_entity",
        429: "rate_limited",
        500: "internal_error",
        503: "service_unavailable",
    }.get(status_code, "error")


def register_error_handlers(app: FastAPI) -> None:
    """Wire the envelope handlers onto ``app``.

    Order matters: :class:`ApiError` is more specific than ``HTTPException``
    so it must be registered first.
    """

    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
    # Fallback to FastAPI's default for anything not matched above.
    app.add_exception_handler(Exception, http_exception_handler)  # type: ignore[arg-type]


__all__ = ["ApiError", "register_error_handlers"]