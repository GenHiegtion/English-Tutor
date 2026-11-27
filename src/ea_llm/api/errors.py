"""One error shape for the whole service: ``{"error": {"code": ..., "message": ...}}``."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
    429: "rate_limited",
    503: "unavailable",
}


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None  # validation errors: field location and reason


class ErrorResponse(BaseModel):
    """Body of every non-2xx response."""

    error: ErrorDetail


class ApiError(Exception):
    """An error the client can act on; mapped to its status code by the handler below."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` entries documenting the error body for ``statuses``."""
    return {status: {"model": ErrorResponse} for status in statuses}


def install_error_handlers(app: FastAPI) -> None:
    """Map every exception to the shared error body; internals are logged, never returned."""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _response(exc.status, exc.code, exc.message, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = STATUS_CODES.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else code
        return _response(exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Only where and why: the rejected input itself may contain the learner's text.
        details = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type")}
            for err in exc.errors()
        ]
        return _response(422, "invalid_request", "request validation failed", details=details)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return _response(500, "internal_error", "internal server error")


def _response(
    status: int,
    code: str,
    message: str,
    *,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, details=details))
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status, headers=headers)
