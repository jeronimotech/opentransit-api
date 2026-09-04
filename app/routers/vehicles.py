import asyncio
import json
import time
import zlib

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ..db import pool
from ..errors import VehicleNotFound
from ..geo import haversine_m
from ..models import VehicleDetail, VehicleFrame
from ..normalize import route_ref_from_db, stop_from_db
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["realtime"])


@router.get("/v1/cities/{city}/vehicles", response_model=VehicleFrame, response_model_exclude_none=True)
async def vehicles(rt: CityRuntime = Depends(city_runtime), routeId: str | None = None,
                   component: str | None = None, bbox: str | None = Query(None, pattern=r"^-?[\d.]+(,-?[\d.]+){3}$")):
    snap = rt.rt.snapshot()
    v = snap["vehicles"]
    if routeId:
        want = rt.city.scoped(routeId)
        v = [x for x in v if x["routeId"] == want]
    if component:
        v = [x for x in v if x["component"] == component]
    if bbox:
        w, s, e, n = (float(t) for t in bbox.split(","))
        v = [x for x in v if w <= x["lon"] <= e and s <= x["lat"] <= n]
    snap["vehicles"], snap["count"] = v, len(v)
    return snap


class StreamFilter:
    """Server-side bbox / route filter for one SSE connection. Vehicles that leave the filter are emitted as
    `removed`, so the client can keep a plain id-keyed map."""

    def __init__(self, bbox: tuple[float, float, float, float] | None, route_ids: set[str] | None):
        self.bbox, self.route_ids = bbox, route_ids
        self.sent: set[str] = set()

    def active(self) -> bool:
        return self.bbox is not None or bool(self.route_ids)

    def match(self, v: dict) -> bool:
        if self.route_ids and v.get("routeId") not in self.route_ids:
            return False
        if self.bbox:
            w, s, e, n = self.bbox
            return w <= v["lon"] <= e and s <= v["lat"] <= n
        return True

    def full(self, frame: dict) -> dict:
        if not self.active():
            return frame
        vs = [v for v in frame["vehicles"] if self.match(v)]
        self.sent = {v["id"] for v in vs}
        return {**frame, "vehicles": vs, "count": len(vs)}

    def delta(self, frame: dict) -> dict:
        if not self.active():
            return frame
        upd = [v for v in frame.get("updated") or [] if self.match(v)]
        gone = [v["id"] for v in frame.get("updated") or [] if not self.match(v) and v["id"] in self.sent]
        removed = [i for i in frame.get("removed") or [] if i in self.sent] + gone
        self.sent |= {v["id"] for v in upd}
        self.sent -= set(removed)
        return {**frame, "updated": upd, "removed": removed, "count": len(self.sent)}


@router.get("/v1/cities/{city}/vehicles/stream")
async def stream(request: Request, rt: CityRuntime = Depends(city_runtime), deltas: bool = True,
                 bbox: str | None = Query(None, pattern=r"^-?[\d.]+(,-?[\d.]+){3}$"),
                 routeIds: str | None = Query(None, description="comma list of route ids")):
    """SSE. First event: full frame. Then deltas (`updated` + `removed`). Keep-alive comment every 25 s.
    `bbox` and `routeIds` filter both the first frame and every delta server-side."""
    cache = rt.rt
    q = cache.subscribe()
    gz = "gzip" in request.headers.get("accept-encoding", "").lower()
    comp = zlib.compressobj(6, zlib.DEFLATED, 31) if gz else None
    flt = StreamFilter(tuple(float(t) for t in bbox.split(",")) if bbox else None,
                       {rt.city.scoped(r.strip()) for r in routeIds.split(",") if r.strip()} if routeIds else None)

    def enc(text: str) -> bytes:
        raw = text.encode()
        return raw if comp is None else comp.compress(raw) + comp.flush(zlib.Z_SYNC_FLUSH)

    async def gen():
        try:
            yield enc(f"data: {json.dumps(flt.full(cache.snapshot()))}\n\n")
            while True:
                if await request.is_disconnected():
                    break
                try:
                    await asyncio.wait_for(q.get(), timeout=25)
                    d = cache.delta_frame() if deltas else None
                    frame = flt.delta(d) if d else flt.full(cache.snapshot())
                    yield enc(f"data: {json.dumps(frame)}\n\n")
                except TimeoutError:
                    yield enc(": keep-alive\n\n")
        finally:
            cache.unsubscribe(q)

    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    if gz:
        headers["Content-Encoding"] = "gzip"
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@router.get("/v1/cities/{city}/vehicles/{vehicleId}", response_model=VehicleDetail)
async def vehicle_detail(vehicleId: str, rt: CityRuntime = Depends(city_runtime)):
    cache, city = rt.rt, rt.city
    v = cache.by_id.get(vehicleId)
    if not v:
        raise VehicleNotFound(f"vehicle '{vehicleId}' is not in the current frame")
    tid, rid = v.get("tripId"), v.get("routeId")
    nxt = cache.trip_next.get(tid or "", {})
    want = [s for s in (v.get("stopId"), nxt.get("stop")) if s]
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", city.id)
        route = await c.fetchrow("SELECT * FROM route WHERE feed_version_id=$1 AND route_id=$2",
                                 fv, rid) if fv and rid else None
        trip = await c.fetchrow("SELECT shape_id, headsign FROM trip WHERE feed_version_id=$1 AND trip_id=$2",
                                fv, tid) if fv and tid else None
        shape = await c.fetchrow("SELECT encoded FROM shape_simplified WHERE feed_version_id=$1 AND shape_id=$2",
                                 fv, trip["shape_id"]) if trip and trip["shape_id"] else None
        stops = {r["stop_id"]: stop_from_db(city, dict(r)) for r in await c.fetch(
            "SELECT * FROM stop WHERE feed_version_id=$1 AND stop_id = ANY($2::text[])", fv, want)} \
            if fv and want else {}
    pts = [[p[0], p[1], p[2]] for p in cache.history.get(vehicleId, [])]
    dist = sum(haversine_m(pts[i - 1][1], pts[i - 1][0], pts[i][1], pts[i][0]) for i in range(1, len(pts)))
    span = (pts[-1][2] - pts[0][2]) if len(pts) > 1 else 0
    eta = nxt.get("eta")
    now = int(time.time())
    alerts = [_public_alert(cache, city, a) for a in cache.alerts_for(rid, want)]
    return {
        **cache.public_vehicle(v),
        "route": route_ref_from_db(city, dict(route)) if route else None,
        "trip": {"id": city.scoped(tid), "resolved": v["tripResolved"], "headsign": trip["headsign"] if trip else None},
        "shape": {"encoded": shape["encoded"], "precision": 5} if shape else None,
        "currentStop": stops.get(v.get("stopId") or ""),
        "nextStop": stops.get(nxt.get("stop") or ""),
        "etaSeconds": (eta - now) if eta else None,
        "delaySeconds": cache.trip_delays.get(tid or ""),
        "history": {"points": pts, "spanSeconds": span, "distanceMeters": round(dist),
                    "avgKmh": round(3.6 * dist / span, 1) if span > 0 else None},
        "alerts": alerts,
    }


def _public_alert(cache, city, a: dict) -> dict:
    from ..rt import iso
    routes = [route_ref_from_db(city, cache.route_index[r]) for r in a["routeIds"] if r in cache.route_index]
    return {**a, "start": iso(a["start"]), "end": iso(a["end"]),
            "routeIds": [city.scoped(r) for r in a["routeIds"]], "stopIds": [city.scoped(s) for s in a["stopIds"]],
            "routes": routes}
