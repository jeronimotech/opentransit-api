"""v2.7 rider reports: wrong data and physical barriers, reported from inside the app."""
from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..db import pool
from ..errors import ApiError
from ..reports import clean_report
from ..runtime import CityRuntime, city_runtime
from .admin import require_admin

router = APIRouter(tags=["reports"])


class TooManyReports(ApiError):
    status, code = 429, "RATE_LIMITED"


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/v1/cities/{city}/reports", status_code=201)
async def create_report(request: Request, rt: CityRuntime = Depends(city_runtime),
                        body: dict = Body(...)):
    """File a report about this city's data or accessibility. Anonymous: the row carries what the
    rider typed and nothing the server could add."""
    limiter = request.app.state.report_limiter
    if not limiter.allow(_client_key(request)):
        raise TooManyReports("too many reports; retry later")
    try:
        row = clean_report(body)
    except ValueError as e:
        raise ApiError(str(e), status=422) from e
    async with pool().acquire() as c:
        rid = await c.fetchval(
            """INSERT INTO rider_report (city, kind, message, stop_id, route_id, app_version, locale, contact)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
            rt.city.id, row["kind"], row["message"], row["stop_id"], row["route_id"],
            row["app_version"], row["locale"], row["contact"])
    return JSONResponse({"id": rid, "status": "new"}, status_code=201,
                        headers={"Cache-Control": "no-store"})


@router.get("/v1/admin/cities/{city}/reports", dependencies=[Depends(require_admin)])
async def list_reports(rt: CityRuntime = Depends(city_runtime),
                       status: str | None = Query(None, description="new | seen | fixed | rejected"),
                       limit: int = Query(100, ge=1, le=500)):
    """What riders have reported, newest first."""
    async with pool().acquire() as c:
        rows = await c.fetch(
            """SELECT id, kind, message, stop_id, route_id, app_version, locale, contact, status, created_at
                 FROM rider_report
                WHERE city=$1 AND ($2::text IS NULL OR status=$2)
                ORDER BY created_at DESC LIMIT $3""", rt.city.id, status, limit)
    return JSONResponse({"reports": [
        {"id": r["id"], "kind": r["kind"], "message": r["message"], "stopId": r["stop_id"],
         "routeId": r["route_id"], "appVersion": r["app_version"], "locale": r["locale"],
         "contact": r["contact"], "status": r["status"], "createdAt": r["created_at"].isoformat()}
        for r in rows]}, headers={"Cache-Control": "no-store"})
