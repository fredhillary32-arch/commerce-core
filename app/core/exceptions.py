import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


def constraint_name(error: IntegrityError) -> str:
    """The PostgreSQL constraint a failed write violated, if it reported one.

    Every module that recovers from an IntegrityError has to ask this same
    question first, because the recovery is only ever legitimate for one
    specific constraint - a duplicate cart line may be retried as an
    increment, but an unrelated violation must not be. It lived as a private
    copy in the catalogue, cart and inventory services; the order service
    would have been the fourth, which is where duplication stops being
    cheaper than a shared name.

    Returns "" when the driver reported no constraint, so callers can
    compare without a None check and still fail closed: "" never equals a
    real constraint name, so an unrecognised error is re-raised.
    """
    diagnostic = getattr(error.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None) or ""


class AppException(Exception):
    """Base class for all application-raised errors.

    Routers should never raise HTTPException directly; they should raise
    (or let services raise) a subclass of AppException instead, so every
    error response in the API shares one structured shape.
    """

    status_code: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        # A category's default code is often too coarse for the caller to act
        # on: an authentication failure may be a bad password, a disabled
        # account or a stale token. Raisers narrow it without needing a new
        # exception class per case.
        if code is not None:
            self.code = code


class ValidationError(AppException):
    status_code = 422
    code = "VALIDATION_ERROR"


class AuthenticationError(AppException):
    status_code = 401
    code = "AUTHENTICATION_ERROR"


class AuthorizationError(AppException):
    status_code = 403
    code = "AUTHORIZATION_ERROR"


class NotFoundError(AppException):
    status_code = 404
    code = "RESOURCE_NOT_FOUND"


class ConflictError(AppException):
    status_code = 409
    code = "CONFLICT"


class BusinessRuleError(AppException):
    status_code = 422
    code = "BUSINESS_RULE_VIOLATION"


class InfrastructureError(AppException):
    status_code = 503
    code = "INFRASTRUCTURE_ERROR"


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _format_validation_message(exc: RequestValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(segment) for segment in error["loc"] if segment != "body")
        parts.append(f"{location}: {error['msg']}" if location else error["msg"])
    return "; ".join(parts)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def _handle_app_exception(_: Request, exc: AppException) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(422, ValidationError.code, _format_validation_message(exc))

    @app.exception_handler(Exception)
    async def _handle_unexpected_exception(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception while processing request", exc_info=exc)
        return _error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")
