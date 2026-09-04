"""
Static GTFS ingest, per city.

Only routes, trips, stops, shapes and feed_info are loaded into Postgres. stop_times.txt is streamed
once (never stored) to learn which routes serve each stop; that is enough for search, nearby stops and
the network map. Everything schedule-related is answered by OpenTripPlanner, which has the full feed.

Ported and generalized from SIRCI Live (TransMilenio, 2026) — see NOTICE.md.
"""
import collections
import csv
import hashlib
import io
import json
import logging
import re
import unicodedata
import zipfile

import httpx

from .cities import City
from .config import settings
from .db import pool
from .features import ServiceIndex, accessibility_unverified, hms_to_seconds
from .geo import encode_polyline, rdp
from .network_dedupe import ShapeIn, dedupe_shapes

log = logging.getLogger("ot.static")
NEEDED = ("routes.txt", "trips.txt", "stops.txt")


def normalize_name(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s.lower()).strip()


def _rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))


def _scan_stop_times(z: zipfile.ZipFile, trip2route: dict[str, str]
                     ) -> tuple[dict[str, set[str]], dict[str, tuple[str, str]]]:
    """Stream stop_times.txt once (can be 500 MB). Keeps only
    stop_id -> {route_id} and trip_id -> (first departure, last departure) as 'HH:MM:SS' strings."""
    stop_routes: dict[str, set[str]] = collections.defaultdict(set)
    span: dict[str, list[str]] = {}
    with z.open("stop_times.txt") as fh:
        txt = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
        header = next(csv.reader([txt.readline()]))
        i_trip, i_stop = header.index("trip_id"), header.index("stop_id")
        i_dep = header.index("departure_time") if "departure_time" in header else header.index("arrival_time")
        need = max(i_trip, i_stop, i_dep)
        simple = all("," not in h and '"' not in h for h in header)

        def take(parts: list[str]) -> None:
            tid = parts[i_trip]
            r = trip2route.get(tid)
            if not r:
                return
            stop_routes[parts[i_stop]].add(r)
            t = parts[i_dep]
            if not t:
                return
            if len(t) == 7:            # 'H:MM:SS' -> zero-pad so string comparison works
                t = "0" + t
            sp = span.get(tid)
            if sp is None:
                span[tid] = [t, t]
            else:
                if t < sp[0]:
                    sp[0] = t
                if t > sp[1]:
                    sp[1] = t

        if simple:
            for line in txt:
                parts = line.rstrip("\r\n").split(",")
                if len(parts) <= need or '"' in line:
                    continue
                take(parts)
        else:
            for parts in csv.reader(txt):
                if len(parts) > need:
                    take(parts)
    return stop_routes, {k: (v[0], v[1]) for k, v in span.items()}


def _service_windows(trip_span: dict[str, tuple[str, str]], trip2route: dict[str, str],
                     trip2service: dict[str, str]) -> list[tuple[str, str, int, int]]:
    """(route_id, service_id, first_dep_s, last_dep_s) aggregated over the trips of each pair."""
    agg: dict[tuple[str, str], list[int]] = {}
    for tid, (first, last) in trip_span.items():
        rid, sid = trip2route.get(tid), trip2service.get(tid)
        if not rid or sid is None:
            continue
        f, la = hms_to_seconds(first), hms_to_seconds(last)
        if f is None or la is None:
            continue
        cur = agg.get((rid, sid))
        if cur is None:
            agg[(rid, sid)] = [f, la]
        else:
            cur[0], cur[1] = min(cur[0], f), max(cur[1], la)
    return [(r, sid, v[0], v[1]) for (r, sid), v in agg.items()]


def _parse_date(s: str | None):
    import datetime as dt
    try:
        return dt.datetime.strptime((s or "").strip(), "%Y%m%d").date()
    except ValueError:
        return None


async def _unchanged_upstream(city: City, cli: httpx.AsyncClient) -> dict | None:
    """Cheap HEAD check: skip the ~100 MB download when Last-Modified matches the active version."""
    try:
        h = await cli.head(city.feeds.gtfs_static_url, timeout=20)
        last_mod = h.headers.get("last-modified")
    except httpx.HTTPError:
        return None
    if not last_mod:
        return None
    async with pool().acquire() as c:
        row = await c.fetchrow(
            """SELECT id FROM feed_version WHERE city=$1 AND is_active
                  AND last_modified = $2::text::timestamptz
                  AND EXISTS (SELECT 1 FROM trip WHERE feed_version_id=feed_version.id)
                  AND EXISTS (SELECT 1 FROM stop WHERE feed_version_id=feed_version.id)""", city.id, last_mod)
    return {"changed": False, "feedVersionId": row["id"], "lastModified": last_mod} if row else None


async def ingest(city: City, force: bool = False) -> dict:
    cfg = settings()
    async with httpx.AsyncClient(timeout=900, follow_redirects=True) as cli:
        if not force and (skip := await _unchanged_upstream(city, cli)):
            log.info("[%s] static unchanged upstream (Last-Modified %s), skipping download",
                     city.id, skip["lastModified"])
            return skip
        r = await cli.get(city.feeds.gtfs_static_url)
        r.raise_for_status()
        blob = r.content
        last_mod = r.headers.get("last-modified")
    sha = hashlib.sha256(blob).hexdigest()

    async with pool().acquire() as c:
        prev = await c.fetchrow("SELECT id, is_active FROM feed_version WHERE city=$1 AND sha256=$2",
                                city.id, sha)
        if prev and prev["is_active"] and not force:
            complete = await c.fetchval(
                """SELECT EXISTS (SELECT 1 FROM trip WHERE feed_version_id=$1)
                   AND EXISTS (SELECT 1 FROM stop WHERE feed_version_id=$1)""", prev["id"])
            if complete:
                log.info("[%s] static unchanged (%s), skipping", city.id, sha[:12])
                return {"changed": False, "sha": sha, "feedVersionId": prev["id"]}

    z = zipfile.ZipFile(io.BytesIO(blob))
    names = set(z.namelist())
    missing = [n for n in NEEDED if n not in names]
    if missing:
        raise RuntimeError(f"GTFS zip is missing {missing}")

    feed_info = next(_rows(z, "feed_info.txt"), {}) if "feed_info.txt" in names else {}
    routes = {r["route_id"]: r for r in _rows(z, "routes.txt")}
    trips: list[tuple] = []
    trip2route: dict[str, str] = {}
    trip2service: dict[str, str] = {}
    shape2route: dict[str, str] = {}
    shape_dirs: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for t in _rows(z, "trips.txt"):
        shape2route.setdefault(t.get("shape_id") or "", t["route_id"])
        if t.get("shape_id") and t.get("direction_id") not in (None, ""):
            shape_dirs[t["shape_id"]][int(t["direction_id"])] += 1
        trip2route[t["trip_id"]] = t["route_id"]
        trip2service[t["trip_id"]] = t.get("service_id") or ""
        d = t.get("direction_id")
        trips.append((t["trip_id"], t["route_id"], t.get("shape_id") or None,
                      t.get("trip_headsign") or None, int(d) if d not in (None, "") else None))

    stop_routes, trip_span = _scan_stop_times(z, trip2route) \
        if cfg.INGEST_STOP_ROUTES and "stop_times.txt" in names else ({}, {})
    # frequency-based trips (e.g. TransMiCable) only carry one template departure in stop_times:
    # widen their span with the frequencies.txt service period.
    if "frequencies.txt" in names:
        for fr in _rows(z, "frequencies.txt"):
            tid, st, en = fr.get("trip_id"), (fr.get("start_time") or "").strip(), (fr.get("end_time") or "").strip()
            if not tid or not st or not en:
                continue
            st, en = ("0" + st if len(st) == 7 else st), ("0" + en if len(en) == 7 else en)
            cur = trip_span.get(tid)
            trip_span[tid] = (min(cur[0], st), max(cur[1], en)) if cur else (st, en)
    windows = _service_windows(trip_span, trip2route, trip2service)
    calendars: list[tuple] = []
    if "calendar.txt" in names:
        for c in _rows(z, "calendar.txt"):
            sd, ed = _parse_date(c.get("start_date")), _parse_date(c.get("end_date"))
            if sd and ed:
                days = [int(c.get(d) or 0) for d in ("monday", "tuesday", "wednesday", "thursday", "friday",
                                                     "saturday", "sunday")]
                calendars.append((c["service_id"], days, sd, ed))
    exceptions: list[tuple] = []
    if "calendar_dates.txt" in names:
        for c in _rows(z, "calendar_dates.txt"):
            d = _parse_date(c.get("date"))
            if d:
                exceptions.append((c["service_id"], d, int(c.get("exception_type") or 1)))

    def comp_of_route(rid: str | None) -> str:
        return city.component_of_agency((routes.get(rid) or {}).get("agency_id"))

    stops = []
    wheelchair_counts: collections.Counter = collections.Counter()
    for st in _rows(z, "stops.txt"):
        try:
            lat, lon = float(st["stop_lat"]), float(st["stop_lon"])
        except (KeyError, ValueError):
            continue
        rids = stop_routes.get(st["stop_id"], set())
        wheelchair_counts[int(st.get("wheelchair_boarding") or 0)] += 1
        comps = collections.Counter(comp_of_route(r) for r in rids)
        stops.append((st["stop_id"], st.get("stop_code") or None, (st.get("stop_name") or st["stop_id"]).strip(),
                      normalize_name(st.get("stop_name")), lat, lon,
                      int(st.get("location_type") or 0), st.get("parent_station") or None,
                      int(st.get("wheelchair_boarding") or 0),
                      comps.most_common(1)[0][0] if comps else None, len(rids)))

    shapes_out, total_in, total_out = [], 0, 0
    if "shapes.txt" in names:
        pts: dict[str, list] = collections.defaultdict(list)
        for p in _rows(z, "shapes.txt"):
            pts[p["shape_id"]].append((int(p["shape_pt_sequence"]), float(p["shape_pt_lon"]),
                                       float(p["shape_pt_lat"])))
        for sid, raw in pts.items():
            raw.sort()
            line = [(x, y) for _, x, y in raw]
            total_in += len(line)
            simp = rdp(line, cfg.SIMPLIFY_TOLERANCE)
            if len(simp) < 2:
                continue
            total_out += len(simp)
            rid = shape2route.get(sid)
            rt = routes.get(rid) or {}
            color = rt.get("route_color") or None
            direction = shape_dirs[sid].most_common(1)[0][0] if shape_dirs.get(sid) else None
            shapes_out.append((sid, rid, comp_of_route(rid), f"#{color}" if color else None,
                               len(simp), encode_polyline(simp), direction))
    # dedupe: one commercial route is often many GTFS routes with near-identical shapes
    dd = dedupe_shapes([
        ShapeIn(sid, rid, f"{comp}|{(routes.get(rid) or {}).get('route_short_name') or rid}|{direction}",
                enc, direction)
        for sid, rid, comp, _color, _n, enc, direction in shapes_out])
    n_canonical = sum(1 for o in dd.values() if o.is_canonical)

    meta = {"wheelchairUnverified": accessibility_unverified(dict(wheelchair_counts)),
            "wheelchairCounts": {str(k): v for k, v in wheelchair_counts.items()},
            "serviceWindows": len(windows), "calendars": len(calendars), "calendarExceptions": len(exceptions),
            "shapes": len(shapes_out), "canonicalShapes": n_canonical}

    async with pool().acquire() as c, c.transaction():
        fv = await c.fetchval(
            """INSERT INTO feed_version (city, sha256, last_modified, feed_info, n_routes, n_trips, n_stops,
                                         n_shapes, is_active, meta)
               VALUES ($1, $2, $3::text::timestamptz, $4::jsonb, $5, $6, $7, $8, FALSE, $9::jsonb)
               ON CONFLICT (city, sha256) DO UPDATE SET fetched_at = now(), meta = EXCLUDED.meta
               RETURNING id""",
            city.id, sha, last_mod, json.dumps(feed_info), len(routes), len(trips), len(stops), len(shapes_out),
            json.dumps(meta))
        for tbl in ("route", "trip", "stop", "stop_route", "shape_simplified", "route_service_window",
                    "service_calendar", "service_exception"):
            await c.execute(f"DELETE FROM {tbl} WHERE feed_version_id=$1", fv)
        if windows:
            await c.copy_records_to_table(
                "route_service_window", records=[(fv, *w) for w in windows],
                columns=["feed_version_id", "route_id", "service_id", "first_dep_s", "last_dep_s"])
        if calendars:
            await c.executemany(
                "INSERT INTO service_calendar (feed_version_id, service_id, days, start_date, end_date) "
                "VALUES ($1,$2,$3,$4,$5)", [(fv, *cal) for cal in calendars])
        if exceptions:
            await c.copy_records_to_table(
                "service_exception", records=[(fv, *e) for e in exceptions],
                columns=["feed_version_id", "service_id", "date", "exception_type"])
        await c.executemany(
            """INSERT INTO route (feed_version_id, route_id, agency_id, short_name, long_name, route_type,
                                  color, text_color, component) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            [(fv, r["route_id"], r.get("agency_id") or None, r.get("route_short_name") or None,
              r.get("route_long_name") or None, int(r.get("route_type") or 3),
              f"#{r['route_color']}" if r.get("route_color") else None,
              f"#{r['route_text_color']}" if r.get("route_text_color") else None,
              comp_of_route(r["route_id"])) for r in routes.values()])
        await c.copy_records_to_table(
            "trip", records=[(fv, *t) for t in trips],
            columns=["feed_version_id", "trip_id", "route_id", "shape_id", "headsign", "direction_id"])
        await c.executemany(
            """INSERT INTO stop (feed_version_id, stop_id, stop_code, name, name_norm, lat, lon, geog,
                                 location_type, parent_station, wheelchair, component, n_routes)
               VALUES ($1,$2,$3,$4,$5,$6,$7, ST_SetSRID(ST_MakePoint($7,$6),4326)::geography,
                       $8,$9,$10,$11,$12)""",
            [(fv, *s) for s in stops])
        if stop_routes:
            await c.copy_records_to_table(
                "stop_route", records=[(fv, s, r) for s, rs in stop_routes.items() for r in rs],
                columns=["feed_version_id", "stop_id", "route_id"])
        await c.executemany(
            """INSERT INTO shape_simplified (feed_version_id, shape_id, route_id, component, color, n_points,
                                             encoded, direction_id, is_canonical, canonical_shape_id, length_m,
                                             group_key, represents)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
            [(fv, sid, rid, comp, color, n, enc, direction, dd[sid].is_canonical, dd[sid].canonical_shape_id,
              dd[sid].length_m, f"{comp}|{(routes.get(rid) or {}).get('route_short_name') or rid}|{direction}",
              dd[sid].represents or None)
             for sid, rid, comp, color, n, enc, direction in shapes_out])
        await c.execute("UPDATE feed_version SET is_active=FALSE WHERE city=$1 AND is_active", city.id)
        await c.execute("UPDATE feed_version SET is_active=TRUE WHERE id=$1", fv)
        await c.execute(
            """DELETE FROM feed_version WHERE city=$1 AND id NOT IN (
                 SELECT id FROM feed_version WHERE city=$1 ORDER BY fetched_at DESC LIMIT 2)""", city.id)

    log.info("[%s] static ingested v%s · %d routes · %d stops · %d trips · %d shapes, %d canonical (%d→%d pts)",
             city.id, fv, len(routes), len(stops), len(trips), len(shapes_out), n_canonical, total_in, total_out)
    return {"changed": True, "sha": sha, "feedVersionId": fv, "routes": len(routes), "stops": len(stops),
            "trips": len(trips), "shapes": len(shapes_out), "stopRoutes": len(stop_routes), **meta}


async def load_service_index(city: City) -> ServiceIndex:
    """Calendar + per-route windows for the active feed version, kept in memory (a few thousand rows)."""
    idx = ServiceIndex()
    async with pool().acquire() as c:
        fv = await c.fetchrow("SELECT id, meta FROM feed_version WHERE city=$1 AND is_active LIMIT 1", city.id)
        if not fv:
            return idx
        idx.flags = json.loads(fv["meta"]) if isinstance(fv["meta"], str) else (fv["meta"] or {})
        for r in await c.fetch("SELECT route_id, service_id, first_dep_s, last_dep_s FROM route_service_window "
                               "WHERE feed_version_id=$1", fv["id"]):
            idx.windows.setdefault(r["route_id"], []).append((r["service_id"], r["first_dep_s"], r["last_dep_s"]))
        for r in await c.fetch("SELECT service_id, days, start_date, end_date FROM service_calendar "
                               "WHERE feed_version_id=$1", fv["id"]):
            idx.calendar[r["service_id"]] = (tuple(int(x) for x in r["days"]), r["start_date"], r["end_date"])
        for r in await c.fetch("SELECT service_id, date, exception_type FROM service_exception "
                               "WHERE feed_version_id=$1", fv["id"]):
            idx.exceptions[(r["service_id"], r["date"])] = r["exception_type"]
    return idx


async def load_route_index(city: City) -> tuple[dict[str, dict], set[str], dict[str, str]]:
    """(route_id -> route row, known trip_ids, trip_id -> headsign) for the active feed version."""
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", city.id)
        if not fv:
            return {}, set(), {}
        routes = {r["route_id"]: dict(r) for r in await c.fetch(
            "SELECT route_id, agency_id, short_name, long_name, route_type, color, text_color, component "
            "FROM route WHERE feed_version_id=$1", fv)}
        trips = await c.fetch("SELECT trip_id, headsign FROM trip WHERE feed_version_id=$1", fv)
    return routes, {t["trip_id"] for t in trips}, {t["trip_id"]: t["headsign"] for t in trips if t["headsign"]}
