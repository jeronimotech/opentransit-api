import datetime as dt

from fastapi import APIRouter, Depends, Query

from ..db import pool
from ..errors import RouterUnavailable, StopNotFound
from ..models import DeparturesResponse, NearbyResponse, StopDetail
from ..normalize import departure_from_otp, merge_departures, route_ref, route_ref_from_db, stop_from_db, stop_from_otp
from ..otp import DEPARTURES_QUERY, STATION_DEPARTURES_QUERY, STATION_QUERY, STOP_QUERY
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["stops"])


async def _db_stop(rt: CityRuntime, raw_id: str) -> dict | None:
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", rt.city.id)
        if not fv:
            return None
        row = await c.fetchrow("SELECT * FROM stop WHERE feed_version_id=$1 AND stop_id=$2", fv, raw_id)
        return stop_from_db(rt.city, dict(row)) if row else None


async def _db_children(rt: CityRuntime, raw_id: str) -> list[dict]:
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", rt.city.id)
        rows = await c.fetch("SELECT * FROM stop WHERE feed_version_id=$1 AND parent_station=$2 ORDER BY name",
                             fv, raw_id) if fv else []
    return [stop_from_db(rt.city, dict(r)) for r in rows]


async def _otp_stop_or_station(rt: CityRuntime, scoped_id: str, stop_query: str, station_query: str,
                               **variables) -> dict | None:
    """OTP answers `stop(id)` only for platforms/stops; parent stations live behind `station(id)`."""
    data = await rt.otp.graphql(stop_query, {"id": scoped_id, **variables})
    if data.get("stop"):
        return data["stop"]
    data = await rt.otp.graphql(station_query, {"id": scoped_id, **variables})
    return data.get("station")


@router.get("/v1/cities/{city}/stops/nearby", response_model=NearbyResponse)
async def nearby(rt: CityRuntime = Depends(city_runtime), lat: float = Query(...), lon: float = Query(...),
                 radius: int = Query(500, ge=50, le=3000), limit: int = Query(30, ge=1, le=100)):
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", rt.city.id)
        rows = await c.fetch(
            """SELECT stop_id, stop_code, name, lat, lon, location_type, parent_station, wheelchair, component,
                      ST_Distance(geog, ST_SetSRID(ST_MakePoint($3,$2),4326)::geography) AS dist
                 FROM stop
                WHERE feed_version_id=$1 AND location_type <> 2
                  AND ST_DWithin(geog, ST_SetSRID(ST_MakePoint($3,$2),4326)::geography, $4)
                ORDER BY dist LIMIT $5""", fv, lat, lon, radius, limit) if fv else []
    return {"stops": [{**stop_from_db(rt.city, dict(r)), "distanceMeters": int(round(r["dist"]))} for r in rows]}


@router.get("/v1/cities/{city}/stops/{stopId}", response_model=StopDetail)
async def stop_detail(stopId: str, rt: CityRuntime = Depends(city_runtime)):
    base = await _db_stop(rt, rt.city.unscoped(stopId))
    routes, parent, children = [], None, []
    try:
        s = await _otp_stop_or_station(rt, rt.city.scoped(stopId), STOP_QUERY, STATION_QUERY)
        if s:
            if base is None:
                base = stop_from_otp(rt.city, s)
            routes = [rt.with_window(route_ref(rt.city, r)) for r in (s.get("routes") or []) if r]
            parent = stop_from_otp(rt.city, s.get("parentStation"))
            children = [stop_from_otp(rt.city, c) for c in (s.get("stops") or []) if c]
    except RouterUnavailable:
        pass
    if base is None:
        raise StopNotFound(f"stop '{stopId}' not found")
    if not routes:   # OTP down or stop without patterns: fall back to what the static ingest learned
        async with pool().acquire() as c:
            fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", rt.city.id)
            # a station has no stop_times of its own: union the routes of its child stops
            rows = await c.fetch(
                """SELECT DISTINCT r.* FROM stop_route sr
                     JOIN route r ON r.feed_version_id=sr.feed_version_id AND r.route_id=sr.route_id
                    WHERE sr.feed_version_id=$1
                      AND (sr.stop_id=$2 OR sr.stop_id IN (SELECT stop_id FROM stop
                                                           WHERE feed_version_id=$1 AND parent_station=$2))
                    ORDER BY r.short_name""", fv, rt.city.unscoped(stopId)) if fv else []
        routes = [rt.with_window(route_ref_from_db(rt.city, dict(r))) for r in rows]
    if parent is None and base.get("parentStationId"):
        parent = await _db_stop(rt, rt.city.unscoped(base["parentStationId"]))
    if not children and base["locationType"] == "station":
        children = await _db_children(rt, rt.city.unscoped(stopId))
    return {**base, "routes": routes, "parentStation": parent, "children": children}


@router.get("/v1/cities/{city}/stops/{stopId}/departures", response_model=DeparturesResponse)
async def departures(stopId: str, rt: CityRuntime = Depends(city_runtime),
                     limit: int = Query(20, ge=1, le=100), minutes: int = Query(60, ge=5, le=720)):
    s = await _otp_stop_or_station(rt, rt.city.scoped(stopId), DEPARTURES_QUERY, STATION_DEPARTURES_QUERY,
                                   n=limit, range=minutes * 60)
    if not s:
        raise StopNotFound(f"stop '{stopId}' not found")
    stop = await _db_stop(rt, rt.city.unscoped(stopId)) or stop_from_otp(rt.city, s)
    deps = [departure_from_otp(rt.city, st) for st in (s.get("stoptimesWithoutPatterns") or []) if st]
    # OTP does not return the vehicle on a stoptime; our own RT frame knows which bus is on that trip.
    by_trip = {e["tripId"]: e["id"] for e in rt.rt.vehicles if e.get("tripId")}
    for d in deps:
        d["vehicleId"] = by_trip.get(rt.city.unscoped(d["tripId"])) if d.get("tripId") else None
        rt.with_window(d.get("route"))
    deps = merge_departures(deps)[:limit]
    now = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    return {"stop": stop, "generatedAt": now, "departures": deps}
