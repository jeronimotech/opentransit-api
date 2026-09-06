"""
First-party analytics (v1.5): usage + mobility intelligence per city, privacy by design.

What is enforced here (see docs/API.md "Analytics & privacy"):
- events are validated per type; unknown props are dropped; free text is never accepted (only labels of
  selected stops/POIs), addresses are never stored;
- coordinates are replaced by geohash-7 cells (~150 m) before anything touches the database — raw lat/lon
  never reach a row; timestamps are bucketed to 5 minutes;
- session/cohort ids are stored as SHA-256 with a server-side salt that rotates daily (not reversible,
  never logged); IPs are used for in-memory rate limiting only and never persisted;
- every read and export applies a k-anonymity threshold (default 5, admin-editable);
- raw events are kept `retentionDays` (partition drop); aggregates are kept.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import logging
import secrets
import time
from collections import Counter, defaultdict
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import geohash
from .cities import City
from .db import pool

log = logging.getLogger("ot.analytics")

MAX_EVENTS = 50
MAX_BODY_BYTES = 32 * 1024
MAX_INFLATED_BYTES = 256 * 1024
BUCKET_SECONDS = 300
COARSE_DECIMALS = 3            # clients round to 3 decimals; we re-coarsen to geohash-7 anyway

SCREENS = ("home", "planner", "results", "itinerary", "go", "stop", "board", "route", "routes", "locate", "live",
           "alerts", "favorites", "settings", "rental_station", "vehicle", "landing", "about", "city_picker",
           "other")
MODES = ("WALK", "BUS", "RAIL", "SUBWAY", "TRAM", "CABLE_CAR", "FERRY", "BICYCLE", "BIKE_RENTAL",
         "SCOOTER_RENTAL", "CAR", "CAR_ONDEMAND", "TRANSIT", "ONDEMAND", "BICYCLE_RENTAL")
RESULT_TYPES = ("stop", "station", "address", "poi", "place", "rental_station", "street")
LABELLED_RESULT_TYPES = ("stop", "station", "poi", "rental_station")    # labels kept only for these
LAYERS = ("liveVehicles", "network", "zonalNetwork", "pois", "bikeStations", "rental")


# ------------------------------------------------------------------ event schemas (extra props dropped)
class _Props(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AppOpen(_Props):
    coldStart: bool | None = None
    entry: str | None = Field(None, pattern=r"^(home|deeplink|widget|notification)$")


class ScreenView(_Props):
    screen: str = Field(pattern="^(" + "|".join(SCREENS) + ")$")


class SearchSelect(_Props):
    resultType: str = Field(pattern="^(" + "|".join(RESULT_TYPES) + ")$")
    resultId: str | None = Field(None, max_length=80)
    label: str | None = Field(None, max_length=120)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    field: str | None = Field(None, pattern=r"^(from|to|locate)$")
    position: int | None = Field(None, ge=0, le=50)


class PlanRequest(_Props):
    fromLat: float = Field(ge=-90, le=90)
    fromLon: float = Field(ge=-180, le=180)
    toLat: float = Field(ge=-90, le=90)
    toLon: float = Field(ge=-180, le=180)
    fromKind: str | None = Field(None, pattern=r"^(myLocation|stop|address|poi|favorite|map)$")
    toKind: str | None = Field(None, pattern=r"^(myLocation|stop|address|poi|favorite|map)$")
    modes: list[str] = Field([], max_length=8)
    timeType: str | None = Field(None, pattern=r"^(now|depart|arrive)$")
    wheelchair: bool | None = None
    rental: bool | None = None
    onDemand: bool | None = None
    bike: bool | None = None


class PlanResult(_Props):
    count: int = Field(ge=0, le=50)
    bestDurationSeconds: int | None = Field(None, ge=0, le=86400)
    bestTransfers: int | None = Field(None, ge=0, le=20)
    hasRental: bool | None = None
    hasOnDemand: bool | None = None
    latencyMs: int | None = Field(None, ge=0, le=600000)


class ItinerarySelect(_Props):
    index: int | None = Field(None, ge=0, le=50)
    source: str | None = Field(None, pattern=r"^(primary|rental|ondemand)$")
    modes: list[str] = Field([], max_length=8)
    durationSeconds: int | None = Field(None, ge=0, le=86400)
    transfers: int | None = Field(None, ge=0, le=20)
    fareAmount: float | None = Field(None, ge=0, le=10_000_000)
    routeIds: list[str] = Field([], max_length=12)


class GoEvent(_Props):
    durationSeconds: int | None = Field(None, ge=0, le=86400)
    completed: bool | None = None
    legs: int | None = Field(None, ge=0, le=30)


class StopEvent(_Props):
    stopId: str = Field(max_length=80)
    component: str | None = Field(None, max_length=20)


class RouteView(_Props):
    routeId: str = Field(max_length=80)


class LocateQuery(_Props):
    stopId: str = Field(max_length=80)
    routeId: str = Field(max_length=80)
    liveRows: int | None = Field(None, ge=0, le=50)
    scheduledRows: int | None = Field(None, ge=0, le=50)


class Handoff(_Props):
    providerId: str = Field(max_length=40)
    kind: str | None = Field(None, pattern=r"^(taxi|ridehail|mixed)$")
    legIndex: int | None = Field(None, ge=0, le=30)
    hadEstimate: bool | None = None


class RentalStationView(_Props):
    stationId: str = Field(max_length=80)
    networkId: str | None = Field(None, max_length=40)


class Favorite(_Props):
    kind: str = Field(pattern=r"^(place|stop|route)$")
    label: str | None = Field(None, max_length=60)      # kept only when it is exactly "home" or "work"


class AlertView(_Props):
    alertId: str = Field(max_length=80)


class LayerToggle(_Props):
    layer: str = Field(max_length=30)
    on: bool


class ModeToggle(_Props):
    mode: str = Field(max_length=30)
    on: bool


class ErrorEvent(_Props):
    code: str = Field(max_length=40)
    screen: str | None = Field(None, max_length=30)


SCHEMAS: dict[str, type[_Props]] = {
    "app_open": AppOpen, "screen_view": ScreenView, "search_select": SearchSelect, "plan_request": PlanRequest,
    "plan_result": PlanResult, "itinerary_select": ItinerarySelect, "go_start": GoEvent, "go_end": GoEvent,
    "stop_view": StopEvent, "board_view": StopEvent, "route_view": RouteView, "locate_query": LocateQuery,
    "handoff": Handoff, "rental_station_view": RentalStationView, "favorite_add": Favorite,
    "favorite_remove": Favorite, "alert_view": AlertView, "layer_toggle": LayerToggle, "mode_toggle": ModeToggle,
    "error": ErrorEvent,
}


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(max_length=40)
    at: dt.datetime
    props: dict[str, Any] = {}


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessionId: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    cohortId: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    platform: str = Field(pattern=r"^(ios|android|web)$")
    appVersion: str = Field(max_length=32)
    locale: str = Field("es", max_length=10)
    sentAt: dt.datetime | None = None
    events: list[EventIn] = Field(max_length=MAX_EVENTS)


# ------------------------------------------------------------------ privacy primitives
def bucket(t: dt.datetime) -> dt.datetime:
    """Floor to 5 minutes, in UTC."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    t = t.astimezone(dt.UTC)
    secs = (t.minute * 60 + t.second) % BUCKET_SECONDS
    return (t - dt.timedelta(seconds=secs)).replace(microsecond=0)


def cell(lat: float | None, lon: float | None) -> str | None:
    if lat is None or lon is None:
        return None
    return geohash.encode(round(lat, COARSE_DECIMALS), round(lon, COARSE_DECIMALS), 7)


class Hasher:
    """SHA-256(salt(day) + id). The salt is random per UTC day, kept in memory (and optionally in a DB row so
    several API instances agree); it is never logged or returned."""

    def __init__(self, salt_source=None) -> None:
        self._salts: dict[dt.date, str] = {}
        self._source = salt_source           # async callable(day) -> salt, or None for memory-only

    async def salt(self, day: dt.date) -> str:
        s = self._salts.get(day)
        if s is None:
            s = (await self._source(day)) if self._source else secrets.token_hex(32)
            self._salts[day] = s
            for old in [d for d in self._salts if d < day - dt.timedelta(days=2)]:
                self._salts.pop(old, None)
        return s

    async def hash(self, value: str, day: dt.date) -> str:
        return hashlib.sha256(f"{await self.salt(day)}:{value}".encode()).hexdigest()


class RateLimiter:
    """Fixed-window counter per key, in memory only (keys are never persisted)."""

    def __init__(self, limit: int = 60, window_s: int = 60) -> None:
        self.limit, self.window = limit, window_s
        self._hits: dict[str, tuple[int, int]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now or time.time()
        win = int(now // self.window)
        w, n = self._hits.get(key, (win, 0))
        if w != win:
            n = 0
        n += 1
        self._hits[key] = (win, n)
        if len(self._hits) > 10000:                      # cheap cleanup
            self._hits = {k: v for k, v in self._hits.items() if v[0] == win}
        return n <= self.limit


# ------------------------------------------------------------------ validation -> storable rows
def _drop_privacy(etype: str, props: dict) -> dict:
    """Second line of defence after the schema: strip anything that could carry personal data."""
    if etype == "search_select":
        if props.get("resultType") not in LABELLED_RESULT_TYPES:
            props["label"] = None
        if props.get("resultType") not in ("stop", "station", "rental_station"):
            props["resultId"] = None
    if etype in ("favorite_add", "favorite_remove") and props.get("label") not in ("home", "work"):
        props["label"] = None
    return props


async def prepare_rows(city: City, batch: BatchIn, hasher: Hasher, received_at: dt.datetime | None = None
                       ) -> tuple[list[dict], list[int]]:
    """Validate every event; return storable rows (no raw coordinates) and the indexes rejected."""
    rows, rejected = [], []
    received_at = received_at or dt.datetime.now(dt.UTC)
    day = received_at.date()
    session_hash = await hasher.hash(batch.sessionId, day)
    cohort_hash = await hasher.hash(batch.cohortId, day)
    for i, ev in enumerate(batch.events):
        model = SCHEMAS.get(ev.type)
        if model is None:
            rejected.append(i)
            continue
        try:
            props = model.model_validate(ev.props).model_dump(exclude_none=True)
        except ValidationError:
            rejected.append(i)
            continue
        props = _drop_privacy(ev.type, props)
        gh = from_gh = to_gh = None
        if ev.type == "plan_request":
            from_gh = cell(props.pop("fromLat"), props.pop("fromLon"))
            to_gh = cell(props.pop("toLat"), props.pop("toLon"))
        elif ev.type == "search_select":
            gh = cell(props.pop("lat", None), props.pop("lon", None))
        for k in ("lat", "lon", "fromLat", "fromLon", "toLat", "toLon"):
            props.pop(k, None)
        at = bucket(ev.at)
        if abs((received_at - at).total_seconds()) > 7 * 86400:    # implausible clock: drop, never store
            rejected.append(i)
            continue
        rows.append({"city_id": city.id, "at_bucket": at, "received_at": received_at, "type": ev.type,
                     "session_hash": session_hash, "cohort_hash": cohort_hash, "platform": batch.platform,
                     "app_version": batch.appVersion[:32], "locale": batch.locale[:10],
                     "props": {k: v for k, v in props.items() if v is not None},
                     "gh7": gh, "from_gh7": from_gh, "to_gh7": to_gh})
    return rows, rejected


# ------------------------------------------------------------------ aggregation (pure Python, idempotent)
def _local(ts: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    return ts.astimezone(tz)


def aggregate(rows: list[dict], tz: ZoneInfo) -> dict[str, list[dict]]:
    """Roll a list of raw rows into every aggregate table. Deterministic: same rows -> same output."""
    od: Counter = Counter()
    place: Counter = Counter()
    route: dict = defaultdict(lambda: Counter())
    stop: dict = defaultdict(lambda: Counter())
    mode: dict = defaultdict(lambda: Counter())
    search: Counter = Counter()
    provider: dict = defaultdict(lambda: Counter())
    funnel: dict = defaultdict(lambda: Counter())
    sessions: dict = defaultdict(set)
    hours: Counter = Counter()
    platform: dict = defaultdict(lambda: Counter())
    for r in rows:
        t = _local(r["at_bucket"], tz)
        day = t.date()
        hour = t.replace(minute=0, second=0, microsecond=0)
        p = r["props"] or {}
        et = r["type"]
        sessions[day].add(r["session_hash"])
        platform[(day, r["platform"], r["app_version"])]["n"] += 1
        if et == "plan_request":
            if r["from_gh7"] and r["to_gh7"]:
                od[(hour, r["from_gh7"], r["to_gh7"])] += 1
                place[(hour, r["from_gh7"], "origin")] += 1
                place[(hour, r["to_gh7"], "destination")] += 1
            key = "+".join(sorted(m for m in p.get("modes", []) if m in MODES)) or "TRANSIT+WALK"
            mode[(day, key)]["requests"] += 1
            funnel[day]["plan_requests"] += 1
            hours[(day, hour.hour)] += 1
        elif et == "itinerary_select":
            key = "+".join(sorted(m for m in p.get("modes", []) if m in MODES)) or "TRANSIT+WALK"
            mode[(day, key)]["selects"] += 1
            funnel[day]["itinerary_selects"] += 1
            for rid in p.get("routeIds", []):
                route[(day, rid)]["selects"] += 1
        elif et == "search_select":
            if r["gh7"]:
                place[(hour, r["gh7"], "search")] += 1
            search[(day, p.get("resultType"), p.get("resultId"), p.get("label"))] += 1
        elif et == "route_view":
            route[(day, p["routeId"])]["views"] += 1
        elif et == "locate_query":
            route[(day, p["routeId"])]["locates"] += 1
            stop[(day, p["stopId"])]["locates"] += 1
        elif et == "stop_view":
            stop[(day, p["stopId"])]["views"] += 1
        elif et == "board_view":
            stop[(day, p["stopId"])]["boards"] += 1
        elif et == "handoff":
            provider[(day, p["providerId"])]["handoffs"] += 1
            if p.get("hadEstimate"):
                provider[(day, p["providerId"])]["had_estimate"] += 1
        elif et == "app_open":
            funnel[day]["app_opens"] += 1
        elif et == "go_start":
            funnel[day]["go_starts"] += 1
        elif et == "go_end":
            if p.get("completed"):
                funnel[day]["go_completions"] += 1
    for day, s in sessions.items():
        funnel[day]["sessions"] = len(s)
    return {
        "agg_od_hourly": [{"hour": h, "from_gh7": a, "to_gh7": b, "n": n} for (h, a, b), n in od.items()],
        "agg_place_hourly": [{"hour": h, "gh7": g, "kind": k, "n": n} for (h, g, k), n in place.items()],
        "agg_route_daily": [{"day": d, "route_id": rid, "views": c["views"], "selects": c["selects"],
                             "locates": c["locates"]} for (d, rid), c in route.items()],
        "agg_stop_daily": [{"day": d, "stop_id": sid, "views": c["views"], "boards": c["boards"],
                            "locates": c["locates"]} for (d, sid), c in stop.items()],
        "agg_mode_daily": [{"day": d, "mode_set": m, "requests": c["requests"], "selects": c["selects"]}
                           for (d, m), c in mode.items()],
        "agg_search_daily": [{"day": d, "result_type": t, "result_id": i, "label": lb, "n": n}
                             for (d, t, i, lb), n in search.items()],
        "agg_provider_daily": [{"day": d, "provider_id": pid, "handoffs": c["handoffs"],
                                "had_estimate": c["had_estimate"]} for (d, pid), c in provider.items()],
        "agg_funnel_daily": [{"day": d, "app_opens": c["app_opens"], "sessions": c["sessions"],
                              "plan_requests": c["plan_requests"], "itinerary_selects": c["itinerary_selects"],
                              "go_starts": c["go_starts"], "go_completions": c["go_completions"]}
                             for d, c in funnel.items()],
        "agg_hours_daily": [{"day": d, "hour": h, "plan_requests": n} for (d, h), n in hours.items()],
        "agg_platform_daily": [{"day": d, "platform": pl, "app_version": v, "n": c["n"]}
                               for (d, pl, v), c in platform.items()],
    }


DAILY_TABLES = ("agg_route_daily", "agg_stop_daily", "agg_mode_daily", "agg_search_daily", "agg_provider_daily",
                "agg_funnel_daily", "agg_hours_daily", "agg_platform_daily")
HOURLY_TABLES = ("agg_od_hourly", "agg_place_hourly")


# ------------------------------------------------------------------ storage
class AnalyticsStore(Protocol):
    async def insert(self, rows: list[dict]) -> int: ...
    async def rollup(self, city: City) -> dict: ...
    async def fetch(self, table: str, city_id: str, day_from: dt.date, day_to: dt.date) -> list[dict]: ...
    async def od(self, city_id: str, day_from: dt.date, day_to: dt.date, k: int, limit: int, tz: ZoneInfo
                 ) -> dict: ...
    async def places(self, city_id: str, day_from: dt.date, day_to: dt.date, kind: str, k: int, limit: int,
                     tz: ZoneInfo) -> list[dict]: ...
    async def health(self, city_id: str) -> dict: ...


def _touched(rows: list[dict], tz: ZoneInfo) -> tuple[set[dt.date], set[dt.datetime]]:
    days, hours = set(), set()
    for r in rows:
        t = _local(r["at_bucket"], tz)
        days.add(t.date())
        hours.add(t.replace(minute=0, second=0, microsecond=0))
    return days, hours


class MemoryAnalyticsStore:
    """In-memory store (tests / dev without Postgres). Same semantics as the Postgres store."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.aggs: dict[str, dict[str, list[dict]]] = {}      # city -> table -> rows
        self.watermark: dict[str, dt.datetime] = {}
        self.last_rollup: dict[str, dt.datetime] = {}

    async def insert(self, rows: list[dict]) -> int:
        self.rows.extend(rows)
        return len(rows)

    async def rollup(self, city: City) -> dict:
        tz = ZoneInfo(city.timezone)
        wm = self.watermark.get(city.id)
        new = [r for r in self.rows if r["city_id"] == city.id and (wm is None or r["received_at"] > wm)]
        if not new:
            self.last_rollup[city.id] = dt.datetime.now(dt.UTC)
            return {"events": 0, "days": 0}
        days, hours = _touched(new, tz)
        scope = [r for r in self.rows if r["city_id"] == city.id and _local(r["at_bucket"], tz).date() in days]
        fresh = aggregate(scope, tz)
        aggs = self.aggs.setdefault(city.id, {t: [] for t in DAILY_TABLES + HOURLY_TABLES})
        for t in DAILY_TABLES:
            aggs[t] = [x for x in aggs[t] if x["day"] not in days] + [x for x in fresh[t] if x["day"] in days]
        for t in HOURLY_TABLES:
            aggs[t] = [x for x in aggs[t] if x["hour"] not in hours] + [x for x in fresh[t] if x["hour"] in hours]
        self.watermark[city.id] = max(r["received_at"] for r in new)
        self.last_rollup[city.id] = dt.datetime.now(dt.UTC)
        return {"events": len(new), "days": len(days)}

    async def fetch(self, table: str, city_id: str, day_from: dt.date, day_to: dt.date) -> list[dict]:
        rows = self.aggs.get(city_id, {}).get(table, [])
        if table in HOURLY_TABLES:
            return [r for r in rows if day_from <= r["hour"].date() <= day_to]
        return [r for r in rows if day_from <= r["day"] <= day_to]

    async def od(self, city_id, day_from, day_to, k, limit, tz):
        rows = await self.fetch("agg_od_hourly", city_id, day_from, day_to)
        pairs: Counter = Counter()
        for r in rows:
            pairs[(r["from_gh7"], r["to_gh7"])] += r["n"]
        top = sorted(((a, b, n) for (a, b), n in pairs.items() if n >= k), key=lambda x: -x[2])[:limit]
        prow = await self.fetch("agg_place_hourly", city_id, day_from, day_to)
        cells: dict = defaultdict(lambda: Counter())
        for r in prow:
            cells[r["gh7"]][r["kind"]] += r["n"]
        return {"pairs": [{"from_gh7": a, "to_gh7": b, "n": n} for a, b, n in top],
                "cells": [{"gh7": g, "origins": c["origin"], "destinations": c["destination"],
                           "searches": c["search"]} for g, c in cells.items()
                          if max(c["origin"], c["destination"], c["search"]) >= k]}

    async def places(self, city_id, day_from, day_to, kind, k, limit, tz):
        rows = await self.fetch("agg_place_hourly", city_id, day_from, day_to)
        c: Counter = Counter()
        for r in rows:
            if r["kind"] == kind:
                c[r["gh7"]] += r["n"]
        return [{"gh7": g, "n": n} for g, n in c.most_common() if n >= k][:limit]

    async def health(self, city_id: str) -> dict:
        today = dt.datetime.now(dt.UTC).date()
        n = sum(1 for r in self.rows if r["city_id"] == city_id and r["received_at"].date() == today)
        wm = self.watermark.get(city_id)
        lag = int((dt.datetime.now(dt.UTC) - wm).total_seconds()) if wm else None
        lr = self.last_rollup.get(city_id)
        return {"eventsToday": n, "lastRollupAt": lr.isoformat().replace("+00:00", "Z") if lr else None,
                "queueLag": lag}


class PgAnalyticsStore:
    """Postgres store: partitioned raw events, aggregate tables, watermark per city."""

    async def salt_for(self, day: dt.date) -> str:
        """Per-day salt shared by every API instance; generated on first use, never logged."""
        async with pool().acquire() as c:
            s = await c.fetchval("SELECT salt FROM analytics_salt WHERE day=$1", day)
            if s:
                return s
            s = secrets.token_hex(32)
            await c.execute("INSERT INTO analytics_salt (day, salt) VALUES ($1, $2) ON CONFLICT (day) DO NOTHING",
                            day, s)
            return await c.fetchval("SELECT salt FROM analytics_salt WHERE day=$1", day)

    async def ensure_partitions(self, days_ahead: int = 2) -> None:
        today = dt.datetime.now(dt.UTC).date()
        async with pool().acquire() as c:
            for off in range(-1, days_ahead + 1):
                d = today + dt.timedelta(days=off)
                name = f"analytics_event_{d:%Y%m%d}"
                await c.execute(f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF analytics_event "
                                f"FOR VALUES FROM ('{d.isoformat()}') TO ('{(d + dt.timedelta(days=1)).isoformat()}')")

    async def drop_expired(self, retention_days: int) -> list[str]:
        """Drop raw partitions older than the retention (instant; aggregates are untouched)."""
        cutoff = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=retention_days)
        dropped = []
        async with pool().acquire() as c:
            names = await c.fetch("SELECT inhrelid::regclass::text AS name FROM pg_inherits "
                                  "WHERE inhparent = 'analytics_event'::regclass")
            for r in names:
                n = r["name"].strip('"')
                try:
                    d = dt.datetime.strptime(n.rsplit("_", 1)[1], "%Y%m%d").date()
                except ValueError:
                    continue
                if d < cutoff:
                    await c.execute(f"DROP TABLE IF EXISTS {n}")
                    dropped.append(n)
        return dropped

    async def insert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        async with pool().acquire() as c:
            await c.executemany(
                """INSERT INTO analytics_event (city_id, at_bucket, received_at, type, session_hash, cohort_hash,
                                                platform, app_version, locale, props, gh7, from_gh7, to_gh7)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13)""",
                [(r["city_id"], r["at_bucket"], r["received_at"], r["type"], r["session_hash"], r["cohort_hash"],
                  r["platform"], r["app_version"], r["locale"], json.dumps(r["props"]), r["gh7"], r["from_gh7"],
                  r["to_gh7"]) for r in rows])
        return len(rows)

    async def rollup(self, city: City) -> dict:
        tz = ZoneInfo(city.timezone)
        async with pool().acquire() as c:
            wm = await c.fetchval("SELECT watermark FROM analytics_rollup_state WHERE city_id=$1", city.id)
            new = await c.fetch("SELECT at_bucket, received_at FROM analytics_event "
                                "WHERE city_id=$1 AND received_at > $2",
                                city.id, wm or dt.datetime(1970, 1, 1, tzinfo=dt.UTC))
            if not new:
                await c.execute("INSERT INTO analytics_rollup_state (city_id, watermark, last_rollup_at) "
                                "VALUES ($1, $2, now()) ON CONFLICT (city_id) DO UPDATE SET last_rollup_at=now()",
                                city.id, wm or dt.datetime(1970, 1, 1, tzinfo=dt.UTC))
                return {"events": 0, "days": 0}
            days, hours = _touched([dict(r) for r in new], tz)
            # recompute every touched local day from the raw rows (idempotent: delete + insert per key)
            lo = min(days) - dt.timedelta(days=1)
            hi = max(days) + dt.timedelta(days=2)
            raw = await c.fetch("SELECT city_id, at_bucket, received_at, type, session_hash, cohort_hash, platform, "
                                "app_version, locale, props, gh7, from_gh7, to_gh7 FROM analytics_event "
                                "WHERE city_id=$1 AND at_bucket >= $2 AND at_bucket < $3", city.id,
                                dt.datetime.combine(lo, dt.time.min, tzinfo=dt.UTC),
                                dt.datetime.combine(hi, dt.time.min, tzinfo=dt.UTC))
            rows = []
            for r in raw:
                d = dict(r)
                d["props"] = d["props"] if isinstance(d["props"], dict) else json.loads(d["props"] or "{}")
                if _local(d["at_bucket"], tz).date() in days:
                    rows.append(d)
            fresh = aggregate(rows, tz)
            day_list = sorted(days)
            hour_list = sorted(hours)
            async with c.transaction():
                for t in DAILY_TABLES:
                    await c.execute(f"DELETE FROM {t} WHERE city_id=$1 AND day = ANY($2::date[])", city.id, day_list)
                for t in HOURLY_TABLES:
                    await c.execute(f"DELETE FROM {t} WHERE city_id=$1 AND hour = ANY($2::timestamptz[])",
                                    city.id, hour_list)
                await self._insert_aggs(c, city.id, fresh, days, hours)
                await c.execute("INSERT INTO analytics_rollup_state (city_id, watermark, last_rollup_at) "
                                "VALUES ($1, $2, now()) ON CONFLICT (city_id) DO UPDATE SET "
                                "watermark=EXCLUDED.watermark, last_rollup_at=now()",
                                city.id, max(r["received_at"] for r in new))
        return {"events": len(new), "days": len(days)}

    async def _insert_aggs(self, c, city_id: str, fresh: dict, days: set, hours: set) -> None:
        cols = {
            "agg_od_hourly": ("hour", "from_gh7", "to_gh7", "n"),
            "agg_place_hourly": ("hour", "gh7", "kind", "n"),
            "agg_route_daily": ("day", "route_id", "views", "selects", "locates"),
            "agg_stop_daily": ("day", "stop_id", "views", "boards", "locates"),
            "agg_mode_daily": ("day", "mode_set", "requests", "selects"),
            "agg_search_daily": ("day", "result_type", "result_id", "label", "n"),
            "agg_provider_daily": ("day", "provider_id", "handoffs", "had_estimate"),
            "agg_funnel_daily": ("day", "app_opens", "sessions", "plan_requests", "itinerary_selects", "go_starts",
                                 "go_completions"),
            "agg_hours_daily": ("day", "hour", "plan_requests"),
            "agg_platform_daily": ("day", "platform", "app_version", "n"),
        }
        for t, cs in cols.items():
            rows = [r for r in fresh[t] if (r["day"] in days if "day" in r else r["hour"] in hours)]
            if not rows:
                continue
            ph = ", ".join(f"${i + 2}" for i in range(len(cs)))
            await c.executemany(f"INSERT INTO {t} (city_id, {', '.join(cs)}) VALUES ($1, {ph})",
                                [(city_id, *[r[k] for k in cs]) for r in rows])

    async def fetch(self, table: str, city_id: str, day_from: dt.date, day_to: dt.date) -> list[dict]:
        col = "hour" if table in HOURLY_TABLES else "day"
        lo = dt.datetime.combine(day_from, dt.time.min, tzinfo=dt.UTC) - dt.timedelta(days=1)
        hi = dt.datetime.combine(day_to + dt.timedelta(days=2), dt.time.min, tzinfo=dt.UTC)
        async with pool().acquire() as c:
            if col == "day":
                rows = await c.fetch(f"SELECT * FROM {table} WHERE city_id=$1 AND day BETWEEN $2 AND $3",
                                     city_id, day_from, day_to)
            else:
                rows = await c.fetch(f"SELECT * FROM {table} WHERE city_id=$1 AND hour >= $2 AND hour < $3",
                                     city_id, lo, hi)
        return [dict(r) for r in rows]

    async def od(self, city_id, day_from, day_to, k, limit, tz):
        lo, hi = _hour_range(day_from, day_to, tz)
        async with pool().acquire() as c:
            pairs = await c.fetch("SELECT from_gh7, to_gh7, SUM(n)::int AS n FROM agg_od_hourly WHERE city_id=$1 "
                                  "AND hour >= $2 AND hour < $3 GROUP BY from_gh7, to_gh7 HAVING SUM(n) >= $4 "
                                  "ORDER BY n DESC LIMIT $5", city_id, lo, hi, k, limit)
            cells = await c.fetch("SELECT gh7, SUM(n) FILTER (WHERE kind='origin')::int AS origins, "
                                  "SUM(n) FILTER (WHERE kind='destination')::int AS destinations, "
                                  "SUM(n) FILTER (WHERE kind='search')::int AS searches FROM agg_place_hourly "
                                  "WHERE city_id=$1 AND hour >= $2 AND hour < $3 GROUP BY gh7 "
                                  "HAVING GREATEST(SUM(n) FILTER (WHERE kind='origin'), "
                                  "SUM(n) FILTER (WHERE kind='destination'), "
                                  "SUM(n) FILTER (WHERE kind='search')) >= $4 "
                                  "ORDER BY 2 DESC NULLS LAST LIMIT 5000",
                                  city_id, lo, hi, k)
        return {"pairs": [dict(r) for r in pairs],
                "cells": [{"gh7": r["gh7"], "origins": r["origins"] or 0, "destinations": r["destinations"] or 0,
                           "searches": r["searches"] or 0} for r in cells]}

    async def places(self, city_id, day_from, day_to, kind, k, limit, tz):
        lo, hi = _hour_range(day_from, day_to, tz)
        async with pool().acquire() as c:
            rows = await c.fetch("SELECT gh7, SUM(n)::int AS n FROM agg_place_hourly WHERE city_id=$1 AND kind=$2 "
                                 "AND hour >= $3 AND hour < $4 GROUP BY gh7 HAVING SUM(n) >= $5 "
                                 "ORDER BY n DESC LIMIT $6",
                                 city_id, kind, lo, hi, k, limit)
        return [dict(r) for r in rows]

    async def health(self, city_id: str) -> dict:
        async with pool().acquire() as c:
            n = await c.fetchval("SELECT count(*) FROM analytics_event WHERE city_id=$1 AND received_at >= "
                                 "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'", city_id)
            st = await c.fetchrow("SELECT watermark, last_rollup_at FROM analytics_rollup_state WHERE city_id=$1",
                                  city_id)
            newest = await c.fetchval("SELECT max(received_at) FROM analytics_event WHERE city_id=$1", city_id)
        wm = st["watermark"] if st else None
        lag = int((newest - wm).total_seconds()) if (newest and wm and newest > wm) else (0 if wm else None)
        lr = st["last_rollup_at"] if st else None
        return {"eventsToday": int(n or 0), "lastRollupAt": lr.isoformat().replace("+00:00", "Z") if lr else None,
                "queueLag": lag}


def _hour_range(day_from: dt.date, day_to: dt.date, tz: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    lo = dt.datetime.combine(day_from, dt.time.min, tzinfo=tz)
    hi = dt.datetime.combine(day_to + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
    return lo, hi


# ------------------------------------------------------------------ queries (k applied) and shaping
def _sum(rows: list[dict], key: str) -> int:
    return int(sum(r.get(key) or 0 for r in rows))


def camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(w.capitalize() for w in rest)


def _camel_dict(d: dict) -> dict:
    return {camel(k): v for k, v in d.items()}


def _top(rows: list[dict], group: tuple[str, ...], metrics: tuple[str, ...], k: int,
         k_metric: str | tuple[str, ...], limit: int, names: dict | None = None) -> list[dict]:
    """Group + sum; keep a row only when its k-metric (any of them, when a tuple) reaches the threshold."""
    k_metrics = (k_metric,) if isinstance(k_metric, str) else k_metric
    sort_key = k_metrics[0]
    acc: dict = defaultdict(lambda: Counter())
    for r in rows:
        acc[tuple(r[g] for g in group)].update({m: r.get(m) or 0 for m in metrics})
    out = []
    for key, c in acc.items():
        if max(c[m] for m in k_metrics) < k:
            continue
        item = dict(zip(group, key, strict=True)) | {m: int(c[m]) for m in metrics}
        if names and key[0] in names:
            item.update(names[key[0]])
        out.append(item)
    out.sort(key=lambda x: -x[sort_key])
    return [_camel_dict(x) for x in out[:limit]]


class AnalyticsQueries:
    def __init__(self, store: AnalyticsStore, city: City) -> None:
        self.store, self.city = store, city
        self.tz = ZoneInfo(city.timezone)
        self.k = city.config.analytics.k_threshold

    async def funnel_rows(self, f: dt.date, t: dt.date) -> list[dict]:
        return await self.store.fetch("agg_funnel_daily", self.city.id, f, t)

    async def summary(self, f: dt.date, t: dt.date) -> dict:
        span = (t - f).days + 1
        pf, pt = f - dt.timedelta(days=span), f - dt.timedelta(days=1)
        cur = await self._totals(f, t)
        prev = await self._totals(pf, pt)
        modes = _top(await self.store.fetch("agg_mode_daily", self.city.id, f, t), ("mode_set",),
                     ("requests", "selects"), self.k, ("requests", "selects"), 8)
        routes = _top(await self.store.fetch("agg_route_daily", self.city.id, f, t), ("route_id",),
                      ("views", "selects", "locates"), self.k, ("selects", "views", "locates"), 5)
        stops = _top(await self.store.fetch("agg_stop_daily", self.city.id, f, t), ("stop_id",),
                     ("views", "boards", "locates"), self.k, ("views", "boards", "locates"), 5)
        platforms = _top(await self.store.fetch("agg_platform_daily", self.city.id, f, t), ("platform",), ("n",),
                         0, "n", 3)
        versions = _top(await self.store.fetch("agg_platform_daily", self.city.id, f, t),
                        ("platform", "app_version"), ("n",), self.k, "n", 10)
        return {"period": {"from": f.isoformat(), "to": t.isoformat(), "days": span},
                "previous": {"from": pf.isoformat(), "to": pt.isoformat()},
                "kpis": cur, "totals": cur, "previousTotals": prev,
                "delta": {k: (cur[k] - prev[k]) for k in cur},
                "topModes": modes, "topRoutes": routes, "topStops": stops,
                "platforms": platforms, "versions": versions, "kThreshold": self.k}

    async def _totals(self, f: dt.date, t: dt.date) -> dict:
        fr = await self.funnel_rows(f, t)
        pr = await self.store.fetch("agg_provider_daily", self.city.id, f, t)
        return {"sessions": _sum(fr, "sessions"), "appOpens": _sum(fr, "app_opens"),
                "planRequests": _sum(fr, "plan_requests"), "itinerarySelects": _sum(fr, "itinerary_selects"),
                "goStarts": _sum(fr, "go_starts"), "goCompletions": _sum(fr, "go_completions"),
                "handoffs": _sum(pr, "handoffs"), "activeDays": len({r["day"] for r in fr if r["sessions"]})}

    async def od(self, f: dt.date, t: dt.date, limit: int, k: int | None) -> dict:
        k = max(self.k, k or self.k)
        raw = await self.store.od(self.city.id, f, t, k, limit, self.tz)
        features = []
        for c in raw["cells"]:
            lat, lon = geohash.center(c["gh7"])
            features.append({"type": "Feature",
                             "geometry": {"type": "Polygon", "coordinates": [geohash.polygon(c["gh7"])]},
                             "properties": {"gh7": c["gh7"], "origins": c["origins"], "destinations": c["destinations"],
                                            "searches": c["searches"], "center": [lon, lat]}})
        pairs = []
        for p in raw["pairs"]:
            a, b = geohash.center(p["from_gh7"]), geohash.center(p["to_gh7"])
            pairs.append({"fromGh7": p["from_gh7"], "toGh7": p["to_gh7"], "fromCenter": {"lat": a[0], "lon": a[1]},
                          "toCenter": {"lat": b[0], "lon": b[1]}, "n": int(p["n"])})
        return {"cells": {"type": "FeatureCollection", "features": features}, "pairs": pairs, "kThreshold": k}

    async def places(self, f: dt.date, t: dt.date, kind: str, limit: int, names: dict | None = None) -> list[dict]:
        rows = await self.store.places(self.city.id, f, t, kind, self.k, limit, self.tz)
        out = []
        for r in rows:
            lat, lon = geohash.center(r["gh7"])
            out.append({"gh7": r["gh7"], "center": {"lat": lat, "lon": lon}, "n": int(r["n"])})
        return out

    async def routes(self, f, t, limit, names=None):
        return _top(await self.store.fetch("agg_route_daily", self.city.id, f, t), ("route_id",),
                    ("views", "selects", "locates"), self.k, ("selects", "views", "locates"), limit, names)

    async def stops(self, f, t, limit, names=None):
        return _top(await self.store.fetch("agg_stop_daily", self.city.id, f, t), ("stop_id",),
                    ("views", "boards", "locates"), self.k, ("views", "boards", "locates"), limit, names)

    async def modes(self, f, t, limit=50):
        return _top(await self.store.fetch("agg_mode_daily", self.city.id, f, t), ("mode_set",),
                    ("requests", "selects"), self.k, ("requests", "selects"), limit)

    async def searches(self, f, t, limit):
        return _top(await self.store.fetch("agg_search_daily", self.city.id, f, t),
                    ("result_type", "result_id", "label"), ("n",), self.k, "n", limit)

    async def providers(self, f, t):
        return _top(await self.store.fetch("agg_provider_daily", self.city.id, f, t), ("provider_id",),
                    ("handoffs", "had_estimate"), self.k, "handoffs", 50)

    async def funnel(self, f, t):
        rows = sorted(await self.funnel_rows(f, t), key=lambda r: r["day"])
        keys = ("app_opens", "sessions", "plan_requests", "itinerary_selects", "go_starts", "go_completions")
        return {"days": [{"day": r["day"].isoformat(), **{camel(k): int(r.get(k) or 0) for k in keys}}
                         for r in rows],
                "totals": {camel(k): _sum(rows, k) for k in keys}}

    async def hours(self, f, t):
        rows = await self.store.fetch("agg_hours_daily", self.city.id, f, t)
        grid = [[0] * 24 for _ in range(7)]
        for r in rows:
            grid[r["day"].weekday()][int(r["hour"])] += int(r["plan_requests"] or 0)
        names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return {"weekdays": names, "hours": list(range(24)), "planRequests": grid,
                "rows": [{"weekday": names[w], "hour": h, "planRequests": grid[w][h]}
                         for w in range(7) for h in range(24) if grid[w][h]]}

    async def export_csv(self, dataset: str, f: dt.date, t: dt.date) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        if dataset == "od":
            d = await self.od(f, t, 5000, None)
            w.writerow(["fromGh7", "toGh7", "fromLat", "fromLon", "toLat", "toLon", "n"])
            for p in d["pairs"]:
                w.writerow([p["fromGh7"], p["toGh7"], p["fromCenter"]["lat"], p["fromCenter"]["lon"],
                            p["toCenter"]["lat"], p["toCenter"]["lon"], p["n"]])
        elif dataset == "routes":
            _rows(w, await self.routes(f, t, 5000), ("routeId", "shortName", "longName", "views", "selects",
                                                     "locates"))
        elif dataset == "stops":
            _rows(w, await self.stops(f, t, 5000), ("stopId", "name", "views", "boards", "locates"))
        elif dataset == "modes":
            _rows(w, await self.modes(f, t, 5000), ("modeSet", "requests", "selects"))
        elif dataset == "searches":
            _rows(w, await self.searches(f, t, 5000), ("resultType", "resultId", "label", "n"))
        elif dataset == "providers":
            _rows(w, await self.providers(f, t), ("providerId", "handoffs", "hadEstimate"))
        elif dataset == "funnel":
            fu = await self.funnel(f, t)
            _rows(w, fu["days"], ("day", "appOpens", "sessions", "planRequests", "itinerarySelects", "goStarts",
                                  "goCompletions"))
        elif dataset == "hours":
            h = await self.hours(f, t)
            w.writerow(["weekday", "hour", "planRequests"])
            for i, wd in enumerate(h["weekdays"]):
                for hr in range(24):
                    w.writerow([wd, hr, h["planRequests"][i][hr]])
        else:
            raise ValueError("unknown dataset")
        return buf.getvalue()


def _rows(w, rows: list[dict], cols: tuple[str, ...]) -> None:
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c) for c in cols])
