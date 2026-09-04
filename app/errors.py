"""Error envelope: every error is `{"error": {"code": ..., "message": ...}}`."""
from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    status = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status


class CityNotFound(ApiError):
    status, code = 404, "CITY_NOT_FOUND"


class StopNotFound(ApiError):
    status, code = 404, "STOP_NOT_FOUND"


class RouteNotFound(ApiError):
    status, code = 404, "ROUTE_NOT_FOUND"


class VehicleNotFound(ApiError):
    status, code = 404, "VEHICLE_NOT_FOUND"


class RouterUnavailable(ApiError):
    status, code = 502, "ROUTER_UNAVAILABLE"


class Unauthorized(ApiError):
    status, code = 401, "UNAUTHORIZED"


def envelope(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return envelope(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("query", "path", "body"))
        msg = f"{loc}: {first.get('msg', 'invalid request')}" if loc else "invalid request"
        return envelope(422, "BAD_REQUEST", msg)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException):
        code = {401: "UNAUTHORIZED", 404: "NOT_FOUND"}.get(exc.status_code, "BAD_REQUEST")
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        return envelope(exc.status_code, code, detail)
