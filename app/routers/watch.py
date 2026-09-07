"""
v1.7 watch summary: one compact call that fills a watch face.

A watch has a tiny screen, a slow radio and a battery to protect, so this endpoint is the opposite of the
rest of the API: no geometry, no polylines, no long names, minutes instead of timestamps, and a short cache
so a complication refreshing every minute is nearly free.
"""
from __future__ import annotations

import datetime as dt
import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..db import pool
from ..models import WatchSummaryResponse
from ..normalize import departure_from_otp, merge_departures
from ..otp import DEPARTURES_QUERY, STATION_DEPARTURES_QUERY
from ..runtime import CityRuntime, city_runtime
from .stops import _otp_stop_or_station

router = APIRouter(tags=["wearables"])

CACHE_TTL_S = 15
NAME_MAX = 24                 # what fits on a 45 mm watch face without eliding mid-word
NEXT_PER_ROUTE = 2
MAX_ROUTES_PER_STOP = 3


def truncate(name: str | None, limit: int = NAME_MAX) -> str:
    """Prefer cutting at a word boundary; a watch shows "Portal Norte" better than "Portal Norte - Unice…"."""
    text = (name or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")].rstrip()
    return cut + "…"


def _minutes(iso_time: str, now_ts: float) -> int:
    t = dt.datetime.fromisoformat(iso_time.replace("Z", "+00:00")).timestamp()
    return max(0, int(round((t - now_ts) / 60)))


def compact_rows(deps: list[dict], now_ts: float, *, routes_filter: set[str] | None) -> list[dict]:
    """Departures -> at most three routes, each with its next couple of minutes values."""
    by_route: dict[str, dict] = {}
    for d in deps:
        ref = d.get("route") or {}
        rid = ref.get("id")
        if not rid or (routes_filter and rid not in routes_filter):
            continue
        row = by_route.setdefault(rid, {"routeId": rid, "shortName": ref.get("shortName"),
                                        "color": ref.get("color"), "next": []})
        if len(row["next"]) >= NEXT_PER_ROUTE:
            continue
        t = d.get("realtimeTime") or d["scheduledTime"]
        row["next"].append({"minutes": _minutes(t, now_ts), "realtime": bool(d.get("realtime"))})
    rows = [r for r in by_route.values() if r["next"]]
    rows.sort(key=lambda r: r["next"][0]["minutes"])
    return rows[:MAX_ROUTES_PER_STOP]


class WatchCache:
    def __init__(self, ttl_s: int = CACHE_TTL_S, max_entries: int = 256) -> None:
        self.ttl, self.max_entries = ttl_s, max_entries
        self._items: dict[tuple, tuple[float, dict]] = {}

    def get(self, key: tuple) -> dict | None:
        hit = self._items.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        self._items.pop(key, None)
        return None

    def put(self, key: tuple, value: dict) -> None:
        if len(self._items) >= self.max_entries:
            self._items.pop(min(self._items, key=lambda k: self._items[k][0]), None)
        self._items[key] = (time.time(), value)


async def _nearest_stop_ids(rt: CityRuntime, lat: float, lon: float, limit: int) -> list[tuple[str, int]]:
    """(raw stop id, metres) for the closest stops, so a watch with no favourites still shows something."""
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", rt.city.id)
        if not fv:
            return []
        rows = await c.fetch(
            """SELECT stop_id, ROUND(ST_Distance(geog, ST_MakePoint($2,$3)::geography))::int AS m
                 FROM stop
                WHERE feed_version_id=$1 AND location_type IN (0,1)
                ORDER BY geog <-> ST_MakePoint($2,$3)::geography
                LIMIT $4""", fv, lon, lat, limit)
    return [(r["stop_id"], r["m"]) for r in rows]


@router.get("/v1/cities/{city}/watch/summary", response_model=WatchSummaryResponse)
async def watch_summary(
    request: Request,
    rt: CityRuntime = Depends(city_runtime),
    lat: float | None = Query(None, ge=-90, le=90),
    lon: float | None = Query(None, ge=-180, le=180),
    stops: str | None = Query(None, description="comma-separated favourite stop ids"),
    routes: str | None = Query(None, description="comma-separated route ids to keep"),
    limit: int = Query(3, ge=1, le=6),
):
    """Favourite stops first, then the nearest ones, each with the next couple of departures."""
    city = rt.city
    fav = [s.strip() for s in (stops or "").split(",") if s.strip()][:limit]
    route_filter = {city.scoped(r.strip()) for r in (routes or "").split(",") if r.strip()} or None
    cache: WatchCache = request.app.state.watch_cache
    ckey = (city.id, tuple(fav), tuple(sorted(route_filter or ())), limit,
            None if lat is None else round(lat, 3), None if lon is None else round(lon, 3))
    cached = cache.get(ckey)
    if cached is not None:
        return JSONResponse(cached, headers={"Cache-Control": f"public, max-age={CACHE_TTL_S}"})

    wanted: list[tuple[str, int | None]] = [(s, None) for s in fav]
    if len(wanted) < limit and lat is not None and lon is not None:
        have = {city.unscoped(s) for s, _ in wanted}
        for sid, metres in await _nearest_stop_ids(rt, lat, lon, limit * 2):
            if sid not in have:
                wanted.append((city.scoped(sid), metres))
            if len(wanted) >= limit:
                break

    now_ts = time.time()
    items: list[dict] = []
    for scoped_id, metres in wanted[:limit]:
        s = await _otp_stop_or_station(rt, city.scoped(scoped_id), DEPARTURES_QUERY,
                                       STATION_DEPARTURES_QUERY, n=40, range=90 * 60)
        if not s:
            continue
        deps = merge_departures([departure_from_otp(city, st)
                                 for st in (s.get("stoptimesWithoutPatterns") or []) if st])
        rows = compact_rows(deps, now_ts, routes_filter=route_filter)
        if not rows:
            continue
        items.append({"kind": "route_at_stop" if route_filter else "stop",
                      "stopId": city.scoped(scoped_id),
                      "stopName": truncate(s.get("name")),
                      "component": None, "distanceMeters": metres, "routes": rows})

    body = WatchSummaryResponse(
        generated_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        freshness=rt.freshness(), items=items, alerts=len(rt.rt.active_alerts()),
    ).model_dump(by_alias=True)
    cache.put(ckey, body)
    return JSONResponse(body, headers={"Cache-Control": f"public, max-age={CACHE_TTL_S}"})
