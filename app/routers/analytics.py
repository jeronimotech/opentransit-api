"""v1.5 analytics: public event ingestion + admin aggregates (k-anonymity applied)."""
from __future__ import annotations

import datetime as dt
import gzip
import json

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..analytics import MAX_BODY_BYTES, MAX_INFLATED_BYTES, AnalyticsQueries, BatchIn, prepare_rows
from ..db import pool
from ..errors import ApiError
from ..runtime import CityRuntime, city_runtime
from .admin import require_admin

router = APIRouter(tags=["analytics"])


class TooManyRequests(ApiError):
    status, code = 429, "RATE_LIMITED"


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/v1/cities/{city}/events", status_code=202)
async def ingest_events(request: Request, rt: CityRuntime = Depends(city_runtime)):
    """Fire-and-forget batch. Anonymous, coarse, schema-validated; see docs "Analytics & privacy"."""
    if not rt.city.config.analytics.enabled:
        return JSONResponse({"accepted": 0, "rejected": []}, status_code=202)
    limiter = request.app.state.analytics_limiter
    if not limiter.allow(_client_key(request)):
        raise TooManyRequests("too many event batches; retry later")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise ApiError("batch too large (max 32 KB)", status=413, code="PAYLOAD_TOO_LARGE")
    if request.headers.get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except Exception:  # noqa: BLE001
            raise ApiError("invalid gzip body", status=400) from None
        if len(body) > MAX_INFLATED_BYTES:
            raise ApiError("batch too large", status=413, code="PAYLOAD_TOO_LARGE")
    try:
        batch = BatchIn.model_validate(json.loads(body or b"{}"))
    except (ValidationError, ValueError) as e:
        first = e.errors()[0] if isinstance(e, ValidationError) and e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        raise ApiError(f"{loc}: {first.get('msg')}" if loc else "invalid batch", status=422) from None
    rows, rejected = await prepare_rows(rt.city, batch, request.app.state.analytics_hasher)
    n = await request.app.state.analytics_store.insert(rows)
    return JSONResponse({"accepted": n, "rejected": rejected}, status_code=202)


# ------------------------------------------------------------------ admin (aggregated, k applied)
def _period(f: str | None, t: str | None) -> tuple[dt.date, dt.date]:
    today = dt.datetime.now(dt.UTC).date()
    try:
        day_to = dt.date.fromisoformat(t) if t else today
        day_from = dt.date.fromisoformat(f) if f else day_to - dt.timedelta(days=29)
    except ValueError:
        raise ApiError("from/to must be YYYY-MM-DD", status=422) from None
    if day_from > day_to:
        raise ApiError("from must be <= to", status=422)
    if (day_to - day_from).days > 400:
        raise ApiError("period too long (max 400 days)", status=422)
    return day_from, day_to


def _q(request: Request, rt: CityRuntime) -> AnalyticsQueries:
    return AnalyticsQueries(request.app.state.analytics_store, rt.city)


A = "/v1/admin/cities/{city}/analytics"


@router.get(A + "/summary", dependencies=[Depends(require_admin)])
async def a_summary(request: Request, rt: CityRuntime = Depends(city_runtime)):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    return await _q(request, rt).summary(f, t)


@router.get(A + "/od", dependencies=[Depends(require_admin)])
async def a_od(request: Request, rt: CityRuntime = Depends(city_runtime),
               limit: int = Query(500, ge=1, le=5000), min: int | None = Query(None, ge=1, le=1000)):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    return await _q(request, rt).od(f, t, limit, min)


@router.get(A + "/places", dependencies=[Depends(require_admin)])
async def a_places(request: Request, rt: CityRuntime = Depends(city_runtime),
                   kind: str = Query("origin", pattern=r"^(origin|destination|search)$"),
                   limit: int = Query(100, ge=1, le=5000)):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    return {"kind": kind, "items": await _q(request, rt).places(f, t, kind, limit)}


@router.get(A + "/routes", dependencies=[Depends(require_admin)])
async def a_routes(request: Request, rt: CityRuntime = Depends(city_runtime), limit: int = Query(50, ge=1, le=1000)):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    q = _q(request, rt)
    items = await q.routes(f, t, limit, await _route_names(rt))
    return {"items": items}


@router.get(A + "/stops", dependencies=[Depends(require_admin)])
async def a_stops(request: Request, rt: CityRuntime = Depends(city_runtime), limit: int = Query(50, ge=1, le=1000)):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    items = await _q(request, rt).stops(f, t, limit, await _stop_names(rt))
    return {"items": items}


@router.get(A + "/modes", dependencies=[Depends(require_admin)])
async def a_modes(request: Request, rt: CityRuntime = Depends(city_runtime)):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    return {"items": await _q(request, rt).modes(f, t)}


@router.get(A + "/searches", dependencies=[Depends(require_admin)])
async def a_searches(request: Request, rt: CityRuntime = Depends(city_runtime), limit: int = Query(50, ge=1, le=1000)):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    return {"items": await _q(request, rt).searches(f, t, limit)}


@router.get(A + "/providers", dependencies=[Depends(require_admin)])
async def a_providers(request: Request, rt: CityRuntime = Depends(city_runtime)):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    return {"items": await _q(request, rt).providers(f, t)}


@router.get(A + "/funnel", dependencies=[Depends(require_admin)])
async def a_funnel(request: Request, rt: CityRuntime = Depends(city_runtime)):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    return await _q(request, rt).funnel(f, t)


@router.get(A + "/hours", dependencies=[Depends(require_admin)])
async def a_hours(request: Request, rt: CityRuntime = Depends(city_runtime)):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    return await _q(request, rt).hours(f, t)


@router.get(A + "/export.csv", dependencies=[Depends(require_admin)])
async def a_export(request: Request, rt: CityRuntime = Depends(city_runtime),
                   dataset: str = Query(..., pattern=r"^(od|routes|stops|modes|searches|providers|funnel|hours)$")):
    f, t = _period(request.query_params.get("from"), request.query_params.get("to"))
    text = await _q(request, rt).export_csv(dataset, f, t)
    name = f"{rt.city.id}-analytics-{dataset}-{f}-{t}.csv"
    return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post(A + "/rollup", dependencies=[Depends(require_admin)])
async def a_rollup(request: Request, rt: CityRuntime = Depends(city_runtime)):
    """Run the rollup now (it also runs every 10 minutes)."""
    return await request.app.state.analytics_store.rollup(rt.city)


async def _route_names(rt: CityRuntime) -> dict:
    """route_id (scoped) -> {shortName, longName}; empty when no database (tests)."""
    try:
        async with pool().acquire() as c:
            rows = await c.fetch("SELECT r.route_id, r.short_name, r.long_name FROM route r JOIN feed_version f "
                                 "ON f.id=r.feed_version_id WHERE f.city=$1 AND f.is_active", rt.city.id)
        return {rt.city.scoped(r["route_id"]): {"shortName": r["short_name"], "longName": r["long_name"]}
                for r in rows}
    except Exception:  # noqa: BLE001
        return {}


async def _stop_names(rt: CityRuntime) -> dict:
    try:
        async with pool().acquire() as c:
            rows = await c.fetch("SELECT s.stop_id, s.name FROM stop s JOIN feed_version f ON f.id=s.feed_version_id "
                                 "WHERE f.city=$1 AND f.is_active", rt.city.id)
        return {rt.city.scoped(r["stop_id"]): {"name": r["name"]} for r in rows}
    except Exception:  # noqa: BLE001
        return {}
