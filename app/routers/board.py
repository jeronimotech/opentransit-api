"""
Arrival board and "Ubica tu bus" (stop + route -> next buses).

* board: departures grouped by route/headsign with the next N times each (Maas pattern), plus routes that
  serve the stop but have nothing coming so the client can say "Fuera de horario".
* next: rows built from the live vehicle frame (buses upstream of this stop on the route's patterns),
  labelled live / estimated / scheduled exactly as TransMi App does ("En vivo" / "Por programación").
"""
import datetime as dt
import time

from fastapi import APIRouter, Depends, Query

from ..db import pool
from ..errors import RouteNotFound, StopNotFound
from ..geo import along_track, decode_polyline
from ..models import BoardResponse, NextResponse
from ..normalize import (
    clean_headsign,
    departure_from_otp,
    merge_departures,
    route_ref,
    route_ref_from_db,
    stop_from_otp,
)
from ..otp import DEPARTURES_QUERY, ROUTE_QUERY, STATION_DEPARTURES_QUERY
from ..runtime import CityRuntime, city_runtime
from .stops import _db_children, _db_stop, _otp_stop_or_station

router = APIRouter(tags=["stops"])

_PATTERN_TTL_S = 600
_SPEED_KMH = {"trunk": 22.0, "cable": 15.0, "feeder": 15.0, "dual": 16.0, "zonal": 14.0}
_DWELL_S = 20


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _minutes_until(iso_time: str, now_ts: float) -> int:
    t = dt.datetime.fromisoformat(iso_time.replace("Z", "+00:00")).timestamp()
    return int(round((t - now_ts) / 60))


def group_board(deps: list[dict], per_route: int, now_ts: float) -> list[dict]:
    """Departures -> rows grouped by (route id, headsign), each with its next `per_route` times."""
    rows: dict[tuple[str, str | None], dict] = {}
    for d in deps:
        key = (d["route"]["id"], d.get("headsign"))
        row = rows.setdefault(key, {"route": d["route"], "headsign": d.get("headsign"), "next": []})
        if len(row["next"]) >= per_route:
            continue
        t = d.get("realtimeTime") or d["scheduledTime"]
        row["next"].append({"time": t, "minutes": _minutes_until(t, now_ts), "realtime": bool(d.get("realtime")),
                            "delaySeconds": d.get("delaySeconds"), "tripId": d.get("tripId"),
                            "vehicleId": d.get("vehicleId")})
    return sorted(rows.values(), key=lambda r: (r["next"][0]["minutes"] if r["next"] else 10**6,
                                                r["route"].get("shortName") or ""))


@router.get("/v1/cities/{city}/stops/{stopId}/board", response_model=BoardResponse)
async def board(stopId: str, rt: CityRuntime = Depends(city_runtime),
                minutes: int = Query(60, ge=5, le=720), perRoute: int = Query(3, ge=1, le=10)):
    s = await _otp_stop_or_station(rt, rt.city.scoped(stopId), DEPARTURES_QUERY, STATION_DEPARTURES_QUERY,
                                   n=200, range=minutes * 60)
    if not s:
        raise StopNotFound(f"stop '{stopId}' not found")
    stop = await _db_stop(rt, rt.city.unscoped(stopId)) or stop_from_otp(rt.city, s)
    deps = [departure_from_otp(rt.city, st) for st in (s.get("stoptimesWithoutPatterns") or []) if st]
    by_trip = {e["tripId"]: e["id"] for e in rt.rt.vehicles if e.get("tripId")}
    for d in deps:
        d["vehicleId"] = by_trip.get(rt.city.unscoped(d["tripId"])) if d.get("tripId") else None
    now_ts = time.time()
    rows = group_board(merge_departures(deps), perRoute, now_ts)
    seen = {r["route"]["id"] for r in rows}
    # routes serving the stop with nothing in the window -> empty rows carrying the service window
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", rt.city.id)
        extra = await c.fetch(
            """SELECT DISTINCT r.* FROM stop_route sr
                 JOIN route r ON r.feed_version_id=sr.feed_version_id AND r.route_id=sr.route_id
                WHERE sr.feed_version_id=$1
                  AND (sr.stop_id=$2 OR sr.stop_id IN (SELECT stop_id FROM stop
                                                       WHERE feed_version_id=$1 AND parent_station=$2))
                ORDER BY r.short_name""", fv, rt.city.unscoped(stopId)) if fv else []
    for r in extra:
        ref = route_ref_from_db(rt.city, dict(r))
        if ref["id"] not in seen:
            rows.append({"route": ref, "headsign": None, "next": []})
    for r in rows:
        rt.with_window(r["route"])
    return {"stop": stop, "generatedAt": _now_iso(), "freshness": rt.freshness(), "rows": rows}


# ------------------------------------------------------------------ next buses

async def _patterns(rt: CityRuntime, route_scoped: str) -> tuple[dict, list[dict]]:
    """Route + decoded patterns, cached per route for a few minutes (OTP pattern queries are heavy)."""
    cache = rt.meta.setdefault("patterns", {})
    hit = cache.get(route_scoped)
    if hit and time.time() - hit[0] < _PATTERN_TTL_S:
        return hit[1], hit[2]
    data = await rt.otp.graphql(ROUTE_QUERY, {"id": route_scoped})
    r = data.get("route")
    if not r:
        raise RouteNotFound(f"route '{route_scoped}' not found")
    pats = []
    for p in r.get("patterns") or []:
        if not p:
            continue
        geom = (p.get("patternGeometry") or {}).get("points")
        line = decode_polyline(geom) if geom else []
        stops = [st for st in (p.get("stops") or []) if st]
        along = [along_track(line, st["lon"], st["lat"])[0] if line else i * 500.0 for i, st in enumerate(stops)]
        pats.append({"code": p.get("code"), "headsign": p.get("headsign"), "line": line,
                     "stopIds": [st["gtfsId"] for st in stops], "along": along})
    cache[route_scoped] = (time.time(), r, pats)
    return r, pats


def locate_vehicle(v: dict, pats: list[dict], city) -> tuple[dict | None, int | None, float | None]:
    """(pattern, index of the vehicle's current/next stop, along-track metres). Prefers the RT stop id;
    falls back to geometric projection on the closest pattern."""
    sid = city.scoped(v.get("stopId")) if v.get("stopId") else None
    if sid:
        for p in pats:
            if sid in p["stopIds"]:
                i = p["stopIds"].index(sid)
                along, _ = along_track(p["line"], v["lon"], v["lat"]) if p["line"] else (p["along"][i], 0.0)
                return p, i, along
    best, best_off, best_along = None, float("inf"), 0.0
    for p in pats:
        if not p["line"]:
            continue
        along, off = along_track(p["line"], v["lon"], v["lat"])
        if off < best_off:
            best, best_off, best_along = p, off, along
    if best is None or best_off > 250:
        return None, None, None
    idx = sum(1 for a in best["along"] if a < best_along)   # next stop index by position
    return best, idx, best_along


def next_rows(vehicles: list[dict], pats: list[dict], target_ids: set[str], departures: list[dict], city,
              component: str | None, now_ts: float, limit: int) -> list[dict]:
    """Merge live vehicle rows (upstream of the target stop) with scheduled departures."""
    dep_by_trip = {city.unscoped(d["tripId"]): d for d in departures if d.get("tripId")}
    rows: list[dict] = []
    covered: set[str] = set()
    speed = _SPEED_KMH.get(component or "", 15.0) / 3.6
    for v in vehicles:
        pat, v_idx, v_along = locate_vehicle(v, pats, city)
        if pat is None:
            continue
        t_idx = next((i for i, s in enumerate(pat["stopIds"]) if s in target_ids), None)
        if t_idx is None or v_idx is None or v_idx > t_idx:
            continue
        stops_away = t_idx - v_idx
        dist = max(0.0, pat["along"][t_idx] - (v_along or 0.0))
        dep = dep_by_trip.get(v.get("tripId") or "")
        if dep and dep.get("realtime") and dep.get("realtimeTime"):
            t, source, delay = dep["realtimeTime"], "live", dep.get("delaySeconds")
            mins = _minutes_until(t, now_ts)
        else:
            eta = now_ts + dist / speed + stops_away * _DWELL_S
            t, source, delay = dt.datetime.fromtimestamp(eta, dt.UTC).isoformat().replace("+00:00", "Z"), \
                "estimated", None
            mins = int(round((eta - now_ts) / 60))
        covered.add(v.get("tripId") or "")
        rows.append({"minutes": mins, "time": t, "source": source, "vehicle": v, "stopsAway": stops_away,
                     "distanceMeters": int(round(dist)), "tripId": city.scoped(v.get("tripId")),
                     "delaySeconds": delay})
    rows.sort(key=lambda r: r["minutes"])
    for d in departures:
        if len(rows) >= limit:
            break
        raw = city.unscoped(d["tripId"]) if d.get("tripId") else None
        if raw in covered:
            continue
        t = d.get("realtimeTime") or d["scheduledTime"]
        rows.append({"minutes": _minutes_until(t, now_ts), "time": t,
                     "source": "live" if d.get("realtime") else "scheduled", "vehicle": None,
                     "stopsAway": None, "distanceMeters": None, "tripId": d.get("tripId"),
                     "delaySeconds": d.get("delaySeconds")})
    rows.sort(key=lambda r: r["minutes"])
    return rows[:limit]


@router.get("/v1/cities/{city}/stops/{stopId}/routes/{routeId}/next", response_model=NextResponse)
async def next_buses(stopId: str, routeId: str, rt: CityRuntime = Depends(city_runtime),
                     limit: int = Query(3, ge=1, le=10), minutes: int = Query(90, ge=5, le=720)):
    city = rt.city
    stop = await _db_stop(rt, city.unscoped(stopId))
    if stop is None:
        raise StopNotFound(f"stop '{stopId}' not found")
    target = {stop["id"]}
    if stop["locationType"] == "station":
        target |= {c["id"] for c in await _db_children(rt, city.unscoped(stopId))}
    r, pats = await _patterns(rt, city.scoped(routeId))
    ref = rt.with_window(route_ref(city, r))
    raw_route = city.unscoped(routeId)
    vehicles = [v for v in rt.rt.vehicles if v.get("routeId") == raw_route]
    s = await _otp_stop_or_station(rt, city.scoped(stopId), DEPARTURES_QUERY, STATION_DEPARTURES_QUERY,
                                   n=60, range=minutes * 60)
    deps = [departure_from_otp(city, st) for st in ((s or {}).get("stoptimesWithoutPatterns") or []) if st]
    deps = [d for d in merge_departures(deps) if d["route"]["id"] == city.scoped(routeId)]
    for d in deps:
        d["headsign"] = clean_headsign(d.get("headsign"), ref.get("shortName"))
    serves = any(sid in target for p in pats for sid in p["stopIds"])
    rows = next_rows(vehicles, pats, target, deps, city, ref.get("component"), time.time(), limit) if serves else []
    for row in rows:
        if row["vehicle"] is not None:
            row["vehicle"] = rt.rt.public_vehicle(row["vehicle"])
    return {"stop": stop, "route": ref, "generatedAt": _now_iso(), "freshness": rt.freshness(),
            "servesStop": serves, "vehiclesOnRoute": len(vehicles), "next": rows}
