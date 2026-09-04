"""
GTFS-Realtime poller, one instance per city. One upstream download serves every client.

Ported and generalized from SIRCI Live (TransMilenio, 2026) — see NOTICE.md. Kept in memory: the current
frame, a delta against the previous one, trip delays, next-stop predictions, alerts, and a short
per-vehicle position history (ring buffer) that powers the vehicle detail trail.
"""
import asyncio
import collections
import datetime as dt
import logging
import time

import httpx
from google.transit import gtfs_realtime_pb2 as gtfsrt

from .cities import City
from .config import settings

log = logging.getLogger("ot.rt")


def iso(ts: int | float | None) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(int(ts), dt.UTC).isoformat().replace("+00:00", "Z")


def _txt(field) -> str | None:
    """GTFS-RT wraps text in translations; take the first."""
    return field.translation[0].text.strip() if field.translation else None


def parse_alerts(msg: gtfsrt.FeedMessage) -> tuple[list[dict], dict[str, list[int]], dict[str, list[int]]]:
    out: list[dict] = []
    by_route: dict[str, list[int]] = {}
    by_stop: dict[str, list[int]] = {}
    for e in msg.entity:
        if not e.HasField("alert"):
            continue
        a = e.alert
        per = a.active_period[0] if a.active_period else None
        routes, stops = [], []
        for ie in a.informed_entity:
            if ie.route_id:
                routes.append(ie.route_id)
            if ie.stop_id:
                stops.append(ie.stop_id)
        i = len(out)
        out.append({
            "id": e.id,
            "cause": gtfsrt.Alert.Cause.Name(a.cause) if a.cause else None,
            "effect": gtfsrt.Alert.Effect.Name(a.effect) if a.effect else None,
            "severity": gtfsrt.Alert.SeverityLevel.Name(a.severity_level)
            if a.HasField("severity_level") and a.severity_level else None,
            "header": _txt(a.header_text),
            "description": _txt(a.description_text),
            "url": _txt(a.url),
            "start": (per.start or None) if per else None,
            "end": (per.end or None) if per else None,
            "routeIds": sorted(set(routes)),
            "stopIds": sorted(set(stops)),
        })
        for r in set(routes):
            by_route.setdefault(r, []).append(i)
        for s in set(stops):
            by_stop.setdefault(s, []).append(i)
    return out, by_route, by_stop


def parse_trip_updates(msg: gtfsrt.FeedMessage) -> tuple[dict[str, int], dict[str, dict]]:
    """trip_id -> delay seconds, trip_id -> next stop prediction (first stop_time_update only)."""
    delays: dict[str, int] = {}
    nxt: dict[str, dict] = {}
    for e in msg.entity:
        if not e.HasField("trip_update"):
            continue
        t = e.trip_update
        tid = t.trip.trip_id
        if not tid:
            continue
        if t.HasField("delay"):
            delays[tid] = t.delay
        for su in t.stop_time_update:
            ev = su.arrival if su.HasField("arrival") else (su.departure if su.HasField("departure") else None)
            eta = ev.time if ev is not None and ev.time else None
            if ev is not None and ev.HasField("delay") and tid not in delays:
                delays[tid] = ev.delay
            nxt[tid] = {"stop": su.stop_id or None, "seq": su.stop_sequence or None, "eta": eta}
            break
    return delays, nxt


def parse_positions(msg: gtfsrt.FeedMessage, known_trips: set[str] | None) -> tuple[list[dict], list[int], int]:
    """Vehicles as flat dicts (raw GTFS ids, no feed prefix), sorted entity timestamps, unresolved count."""
    ents, ages, unresolved = [], [], 0
    for e in msg.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        if not v.HasField("position"):
            continue
        tid = v.trip.trip_id or None
        resolved = bool(tid) and (known_trips is None or tid in known_trips)
        if not resolved:
            unresolved += 1
        if v.timestamp:
            ages.append(v.timestamp)
        ents.append({
            "id": v.vehicle.id or v.vehicle.label or e.id,
            "label": v.vehicle.label or None,
            "routeId": v.trip.route_id or None,
            "tripId": tid,
            "tripResolved": resolved,
            "lat": round(v.position.latitude, 5),
            "lon": round(v.position.longitude, 5),
            "bearing": round(v.position.bearing, 1) if v.position.HasField("bearing") else None,
            "ts": v.timestamp or 0,
            "stopId": v.stop_id or None,
            "stopSequence": v.current_stop_sequence or None,
            "occupancy": gtfsrt.VehiclePosition.OccupancyStatus.Name(v.occupancy_status)
            if v.HasField("occupancy_status") else None,
        })
    ages.sort()
    return ents, ages, unresolved


class RTCache:
    """Per-city realtime state. The only thing endpoints read."""

    def __init__(self, city: City):
        self.city = city
        self.vehicles: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.trip_delays: dict[str, int] = {}
        self.trip_next: dict[str, dict] = {}
        self.alerts: list[dict] = []
        self.alerts_by_route: dict[str, list[int]] = {}
        self.alerts_by_stop: dict[str, list[int]] = {}
        self.updated_at: float = 0.0
        self.header_ts: int = 0
        self.entity_ts_p50: int = 0
        self.fetch_ms: int = 0
        self.http_status: int | None = None
        self.n_trip_unresolved: int = 0
        self.known_trips: set[str] | None = None      # None until the static feed is loaded
        self.route_index: dict[str, dict] = {}         # route_id -> row from DB
        self.trip_headsign: dict[str, str] = {}
        self.delta: dict | None = None
        self.seq: int = 0
        self.history: dict[str, collections.deque] = {}
        self._subs: set[asyncio.Queue] = set()

    # ---- static helpers ----
    def set_static(self, routes: dict[str, dict], trips: set[str], headsigns: dict[str, str]) -> None:
        self.route_index, self.known_trips, self.trip_headsign = routes, trips, headsigns

    def component(self, route_id: str | None) -> str | None:
        r = self.route_index.get(route_id or "")
        return r["component"] if r else None

    # ---- alerts ----
    def active_alerts(self, now: float | None = None) -> list[dict]:
        t = int(now or time.time())
        return [a for a in self.alerts
                if (a["start"] is None or a["start"] <= t) and (a["end"] is None or a["end"] >= t)]

    def alerts_for(self, route: str | None, stops) -> list[dict]:
        idx: set[int] = set()
        idx.update(self.alerts_by_route.get(route or "", []))
        for s in stops:
            idx.update(self.alerts_by_stop.get(s or "", []))
        now = int(time.time())
        out = [self.alerts[i] for i in idx
               if not (self.alerts[i]["start"] and self.alerts[i]["start"] > now)
               and not (self.alerts[i]["end"] and self.alerts[i]["end"] < now)]
        return sorted(out, key=lambda a: a["start"] or 0, reverse=True)

    # ---- frames ----
    def health(self) -> dict:
        return {
            "entityAgeP50Seconds": int(time.time() - self.entity_ts_p50) if self.entity_ts_p50 else None,
            "pctTripResolved": round(100 * (1 - self.n_trip_unresolved / max(len(self.vehicles), 1)), 2)
            if self.known_trips is not None and self.vehicles else None,
            "httpStatus": self.http_status,
        }

    def public_vehicle(self, e: dict) -> dict:
        rid = e.get("routeId")
        r = self.route_index.get(rid or "")
        return {
            "id": e["id"], "label": e.get("label"),
            "routeId": self.city.scoped(rid), "routeShortName": r["short_name"] if r else None,
            "tripId": self.city.scoped(e.get("tripId")), "tripResolved": e["tripResolved"],
            "component": r["component"] if r else None,
            "lat": e["lat"], "lon": e["lon"], "bearing": e.get("bearing"), "timestamp": iso(e.get("ts")),
            "stopId": self.city.scoped(e.get("stopId")), "stopSequence": e.get("stopSequence"),
            "occupancy": e.get("occupancy"),
        }

    def _meta(self) -> dict:
        return {"seq": self.seq, "generatedAt": iso(self.updated_at), "feedTimestamp": iso(self.header_ts),
                "count": len(self.vehicles), "health": self.health()}

    def snapshot(self) -> dict:
        return {"type": "full", **self._meta(), "vehicles": [self.public_vehicle(e) for e in self.vehicles]}

    def delta_frame(self) -> dict | None:
        if self.delta is None:
            return None
        return {"type": "delta", **self._meta(), "vehicles": [],
                "updated": [self.public_vehicle(e) for e in self.delta["upd"]], "removed": self.delta["del"]}

    def _compute_delta(self, prev: dict[str, dict], ents: list[dict]) -> dict:
        upd = [e for e in ents if (p := prev.get(e["id"])) is None
               or p["lat"] != e["lat"] or p["lon"] != e["lon"] or p.get("tripId") != e.get("tripId")]
        live = {e["id"] for e in ents}
        return {"upd": upd, "del": [k for k in prev if k not in live]}

    def _record_history(self, ents: list[dict]) -> None:
        n = settings().VEHICLE_HISTORY_POINTS
        for e in ents:
            h = self.history.get(e["id"])
            if h is None:
                h = self.history[e["id"]] = collections.deque(maxlen=n)
            if not h or h[-1][0] != e["lon"] or h[-1][1] != e["lat"]:
                h.append((e["lon"], e["lat"], e["ts"] or int(time.time())))
        if len(self.history) > 3 * max(len(ents), 1000):   # vehicles gone for a long time
            live = {e["id"] for e in ents}
            for k in [k for k in self.history if k not in live]:
                del self.history[k]

    def apply(self, pos: gtfsrt.FeedMessage | None, tu: gtfsrt.FeedMessage | None,
              al: gtfsrt.FeedMessage | None) -> None:
        if al is not None:
            self.alerts, self.alerts_by_route, self.alerts_by_stop = parse_alerts(al)
        if tu is not None:
            self.trip_delays, self.trip_next = parse_trip_updates(tu)
        if pos is None:
            return
        ents, ages, unresolved = parse_positions(pos, self.known_trips)
        self.delta = self._compute_delta(self.by_id, ents) if self.by_id else None
        self.vehicles = ents
        self.by_id = {e["id"]: e for e in ents}
        self.n_trip_unresolved = unresolved
        self.header_ts = pos.header.timestamp
        self.entity_ts_p50 = ages[len(ages) // 2] if ages else 0
        self.updated_at = time.time()
        self.seq += 1
        self._record_history(ents)
        self._publish()

    # ---- pub/sub for SSE ----
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _publish(self) -> None:
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(self.seq)
            except asyncio.QueueFull:
                pass


async def _fetch(cli: httpx.AsyncClient, url: str | None, cache: RTCache, track: bool = False):
    if not url:
        return None
    t0 = time.perf_counter()
    try:
        r = await cli.get(url)
        if track:
            cache.fetch_ms = int((time.perf_counter() - t0) * 1000)
            cache.http_status = r.status_code
        r.raise_for_status()
        m = gtfsrt.FeedMessage()
        m.ParseFromString(r.content)     # feeds are often served as text/plain: always parse as binary
        return m
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] failed to read %s: %s", cache.city.id, url.rsplit("/", 1)[-1], e)
        if track:
            cache.http_status = 0
        return None


async def poll_once(cache: RTCache) -> None:
    f = cache.city.feeds
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as cli:
        pos, tu, al = await asyncio.gather(
            _fetch(cli, f.rt_positions_url, cache, track=True),
            _fetch(cli, f.rt_tripupdates_url, cache),
            _fetch(cli, f.rt_alerts_url, cache))
    cache.apply(pos, tu, al)


async def poller_loop(cache: RTCache, stop: asyncio.Event) -> None:
    every = max(5, cache.city.feeds.poll_seconds)
    while not stop.is_set():
        try:
            await poll_once(cache)
        except Exception:  # noqa: BLE001
            log.exception("[%s] poller cycle failed", cache.city.id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except TimeoutError:
            pass
