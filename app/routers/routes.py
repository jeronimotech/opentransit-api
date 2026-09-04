from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..db import pool
from ..errors import RouteNotFound
from ..models import RouteDetail
from ..normalize import alert_from_otp, pattern_from_otp, route_ref, route_ref_from_db
from ..otp import ROUTE_QUERY
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["routes"])


@router.get("/v1/cities/{city}/routes")
async def list_routes(rt: CityRuntime = Depends(city_runtime), component: str | None = None,
                      q: str | None = Query(None, max_length=60)):
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", rt.city.id)
        rows = await c.fetch(
            """SELECT * FROM route WHERE feed_version_id=$1
                  AND ($2::text IS NULL OR component=$2)
                  AND ($3::text IS NULL OR short_name ILIKE $3 || '%' OR long_name ILIKE '%' || $3 || '%')
                ORDER BY component, short_name""", fv, component, q) if fv else []
    return JSONResponse({"routes": [route_ref_from_db(rt.city, dict(r)) for r in rows]},
                        headers={"Cache-Control": "public, max-age=3600" if not q else "no-store"})


@router.get("/v1/cities/{city}/routes/{routeId}", response_model=RouteDetail)
async def route_detail(routeId: str, rt: CityRuntime = Depends(city_runtime)):
    data = await rt.otp.graphql(ROUTE_QUERY, {"id": rt.city.scoped(routeId)})
    r = data.get("route")
    if not r:
        raise RouteNotFound(f"route '{routeId}' not found")
    base = route_ref(rt.city, r)
    patterns = [pattern_from_otp(rt.city, p, r.get("shortName")) for p in (r.get("patterns") or []) if p]
    patterns.sort(key=lambda p: (p["directionId"] if p["directionId"] is not None else 9, -len(p["stops"])))
    return {**base, "patterns": patterns, "alerts": [alert_from_otp(rt.city, a) for a in (r.get("alerts") or []) if a]}


@router.get("/v1/cities/{city}/network")
async def network(rt: CityRuntime = Depends(city_runtime)):
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", rt.city.id)
        rows = await c.fetch("SELECT shape_id, route_id, component, color, encoded FROM shape_simplified "
                             "WHERE feed_version_id=$1", fv) if fv else []
    return JSONResponse(
        {"feedVersion": str(fv) if fv else None,
         "shapes": [{"id": r["shape_id"], "routeId": rt.city.scoped(r["route_id"]), "component": r["component"],
                     "color": r["color"], "geometry": {"encoded": r["encoded"], "precision": 5}} for r in rows]},
        headers={"Cache-Control": "public, max-age=3600"})
