import asyncio
import time

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..db import pool
from ..errors import RouteNotFound, RouterUnavailable, StopNotFound
from ..models import RouteDetail, SegmentServices
from ..normalize import alert_from_otp, clean_headsign, pattern_from_otp, route_ref, route_ref_from_db
from ..otp import ROUTE_QUERY, STATION_PATTERNS_QUERY, STOP_PATTERNS_QUERY
from ..route_merge import merge_duplicate_routes
from ..runtime import CityRuntime, city_runtime
from ..segments import MAX_ORIGIN_STOPS, equivalent_services, stop_family

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
    refs = [rt.with_window(route_ref_from_db(rt.city, dict(r))) for r in rows]
    # Feeds list the same route many times: Brisbane ships thirteen identical "BRBD · Brisbane City
    # - Airport" rows, 728 of its 1115 entries. Collapsed only on an exact match of what a rider is
    # shown, so Boston's thirty-eight differently-destined "Red Line Shuttle" rows all survive.
    return JSONResponse({"routes": merge_duplicate_routes(refs)},
                        headers={"Cache-Control": "public, max-age=300" if not q else "no-store"})


@router.get("/v1/cities/{city}/routes/{routeId}", response_model=RouteDetail)
async def route_detail(routeId: str, rt: CityRuntime = Depends(city_runtime)):
    data = await rt.otp.graphql(ROUTE_QUERY, {"id": rt.city.scoped(routeId)})
    r = data.get("route")
    if not r:
        raise RouteNotFound(f"route '{routeId}' not found")
    base = rt.with_window(route_ref(rt.city, r))
    patterns = [pattern_from_otp(rt.city, p, r.get("shortName")) for p in (r.get("patterns") or []) if p]
    patterns.sort(key=lambda p: (p["directionId"] if p["directionId"] is not None else 9, -len(p["stops"])))
    return {**base, "patterns": patterns, "alerts": [alert_from_otp(rt.city, a) for a in (r.get("alerts") or []) if a]}


@router.get("/v1/cities/{city}/network")
async def network(rt: CityRuntime = Depends(city_runtime),
                  all: bool = Query(False, description="include non-canonical (duplicate/variant) shapes")):
    """Simplified route geometries for the map. Canonical shapes only by default: exact duplicates and
    variants >= 90 % covered by another shape of the same route are collapsed (their route ids are listed
    in `routeIds`). `?all=true` returns every shape with `canonicalId` for debugging."""
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", rt.city.id)
        rows = await c.fetch(
            """SELECT shape_id, route_id, component, color, encoded, direction_id, is_canonical,
                      canonical_shape_id, length_m, represents
                 FROM shape_simplified WHERE feed_version_id=$1 AND ($2 OR is_canonical)
                ORDER BY component, route_id""", fv, all) if fv else []
    shapes = []
    for r in rows:
        item = {"id": r["shape_id"], "routeId": rt.city.scoped(r["route_id"]),
                "routeIds": [rt.city.scoped(x) for x in (r["represents"] or [r["route_id"]]) if x],
                "component": r["component"], "color": r["color"], "directionId": r["direction_id"],
                "lengthMeters": r["length_m"], "geometry": {"encoded": r["encoded"], "precision": 5}}
        if all:
            item["canonical"] = bool(r["is_canonical"])
            item["canonicalId"] = r["canonical_shape_id"]
        shapes.append(item)
    return JSONResponse({"feedVersion": str(fv) if fv else None, "count": len(shapes), "shapes": shapes},
                        headers={"Cache-Control": "public, max-age=3600"})


# ------------------------------------------------------------------ equivalent services (v2.7)

_STOP_PATTERNS_TTL_S = 600


async def _stop_patterns(rt: CityRuntime, scoped_stop: str) -> list[dict]:
    """The patterns calling at a stop, normalised and cached — OTP pattern queries are heavy."""
    cache = rt.meta.setdefault("stopPatterns", {})
    hit = cache.get(scoped_stop)
    if hit and time.time() - hit[0] < _STOP_PATTERNS_TTL_S:
        return hit[1]
    data = await rt.otp.graphql(STOP_PATTERNS_QUERY, {"id": scoped_stop})
    s = data.get("stop")
    if not s:
        data = await rt.otp.graphql(STATION_PATTERNS_QUERY, {"id": scoped_stop})
        s = data.get("station")
    pats = []
    for p in (s or {}).get("patterns") or []:
        if not p:
            continue
        pats.append({
            "route": rt.with_window(route_ref(rt.city, p.get("route"))),
            "headsign": clean_headsign(p.get("headsign"), (p.get("route") or {}).get("shortName")),
            "directionId": p.get("directionId") if p.get("directionId") in (0, 1) else None,
            "stops": [{"id": st["gtfsId"], "name": st.get("name"), "code": st.get("code")}
                      for st in (p.get("stops") or []) if st],
        })
    cache[scoped_stop] = (time.time(), pats)
    return pats


@router.get("/v1/cities/{city}/segments", response_model=SegmentServices)
async def segment_services(rt: CityRuntime = Depends(city_runtime),
                           from_: str = Query(..., alias="from", description="boarding stop id"),
                           to: str = Query(..., description="alighting stop id"),
                           exclude: str | None = Query(None, description="route already shown (the leg's own)")):
    """Other services that take a rider from one stop to another, so they can board whichever comes
    first. Resolved at station level: an equivalent service may board at another platform of the same
    station, and each answer says which."""
    city = rt.city
    a, b = city.unscoped(from_), city.unscoped(to)
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", city.id)
        rows = await c.fetch(
            """WITH seed AS (SELECT stop_id, parent_station FROM stop
                              WHERE feed_version_id=$1 AND stop_id = ANY($2::text[]))
               SELECT DISTINCT s.stop_id, s.name, s.stop_code, s.location_type, s.parent_station
                 FROM stop s, seed
                WHERE s.feed_version_id=$1
                  AND (s.stop_id = seed.stop_id
                       OR s.stop_id = seed.parent_station
                       OR s.parent_station = seed.stop_id
                       OR (seed.parent_station IS NOT NULL AND s.parent_station = seed.parent_station))""",
            fv, [a, b]) if fv else []
    fam = [dict(r) for r in rows]
    if not any(r["stop_id"] == a for r in fam) or not any(r["stop_id"] == b for r in fam):
        raise StopNotFound(f"stop '{from_ if not any(r['stop_id'] == a for r in fam) else to}' not found")
    origins_raw, dests_raw = stop_family(fam, a), stop_family(fam, b)
    origins = {city.scoped(s) for s in origins_raw}
    dests = {city.scoped(s) for s in dests_raw}
    exclude_ids = {city.scoped(exclude)} if exclude else set()

    # The leg's own boarding stop first, so the cap never drops the platform the rider is standing on.
    ask = [a] + sorted(s for s in origins_raw if s != a and
                       any(r["stop_id"] == s and r["location_type"] == 0 for r in fam))
    try:
        results = await asyncio.gather(*[_stop_patterns(rt, city.scoped(s)) for s in ask[:MAX_ORIGIN_STOPS]])
        patterns = [p for group in results for p in group]
        services = equivalent_services(patterns, origins, dests, exclude_ids)
        match = "pattern"
    except RouterUnavailable:
        # No pattern index: the static ingest knows which routes call at each stop, which can only
        # support the weaker claim that both stops are served by the route.
        async with pool().acquire() as c:
            rows = await c.fetch(
                """SELECT r.* FROM route r
                    WHERE r.feed_version_id=$1
                      AND EXISTS (SELECT 1 FROM stop_route sr WHERE sr.feed_version_id=$1
                                   AND sr.route_id=r.route_id AND sr.stop_id = ANY($2::text[]))
                      AND EXISTS (SELECT 1 FROM stop_route sr WHERE sr.feed_version_id=$1
                                   AND sr.route_id=r.route_id AND sr.stop_id = ANY($3::text[]))
                    ORDER BY r.short_name""",
                fv, list(origins_raw), list(dests_raw)) if fv else []
        services = [rt.with_window(route_ref_from_db(city, dict(r))) for r in rows]
        services = [s for s in services if s["id"] not in exclude_ids]
        match = "stop"

    seed_of = {r["stop_id"]: r for r in fam}
    def _ref(raw: str) -> dict:
        r = seed_of.get(raw) or {}
        return {"id": city.scoped(raw), "name": r.get("name"), "code": r.get("stop_code")}

    return JSONResponse({"from": _ref(a), "to": _ref(b), "match": match, "services": services},
                        headers={"Cache-Control": "public, max-age=300"})
