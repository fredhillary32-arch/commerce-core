import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppException(Exception):
    """Base class for all application-raised errors.

    Routers should never raise HTTPException directly; they should raise
    (or let services raise) a subclass of AppException instead, so every
    error response in the API shares one structured shape.
    """

    status_code: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def _handle_app_exception(_: Request, exc: AppException) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def _handle_unexpected_exception(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception while processing request", exc_info=exc)
        return _error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")
