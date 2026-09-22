"""v2.2 — what the city itself knows about places, beside its stops.

Two things live here. A **cache of the cadastral geocoder's answers** (IDECA): the addresses people look
up repeat, so most searches never reach the upstream, and when it is down the cache still answers. And
the city's **named areas** (Bogotá: 1 231 sectores catastrales, 20 localidades) mirrored from its open
ArcGIS layers, so "Chicó" resolves to the neighbourhood polygon's centre and not to a stop named like it.

Both have an in-memory implementation (tests, dev without a database) and a Postgres one.
"""
import datetime as dt
import json
import logging
from typing import Protocol

import httpx

from .cities import City, PlaceAreas
from .config import settings
from .db import pool
from .gtfs_static import normalize_name

log = logging.getLogger("ot.places")

# an address Catastro assigned does not move; a "not an address" answer may change when they add one
CACHE_HIT_TTL = dt.timedelta(days=180)
CACHE_MISS_TTL = dt.timedelta(days=7)

AREA_NOUNS = {
    "es": {"barrio": "Barrio", "localidad": "Localidad"},
    "en": {"barrio": "Neighbourhood", "localidad": "District"},
}


def area_noun(kind: str, locale: str | None) -> str:
    lang = (locale or "es").split("-")[0].lower()
    return (AREA_NOUNS.get(lang) or AREA_NOUNS["es"]).get(kind, kind)


# ------------------------------------------------------------------ geocode cache


class GeocodeCache(Protocol):
    async def get(self, city_id: str, key: str) -> tuple[bool, dict | None, dt.timedelta | None]:
        """(found, result, age). `result` None with found=True is a remembered miss."""
        ...
    async def put(self, city_id: str, key: str, result: dict | None) -> None: ...
    async def stats(self, city_id: str) -> dict: ...


class MemoryGeocodeCache:
    def __init__(self, now=None) -> None:
        self.rows: dict[tuple[str, str], tuple[dict | None, dt.datetime, int]] = {}
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    async def get(self, city_id, key):
        row = self.rows.get((city_id, key))
        if row is None:
            return False, None, None
        result, at, hits = row
        self.rows[(city_id, key)] = (result, at, hits + 1)
        return True, result, self._now() - at

    async def put(self, city_id, key, result):
        self.rows[(city_id, key)] = (result, self._now(), 1)

    async def stats(self, city_id):
        mine = [v for (c, _), v in self.rows.items() if c == city_id]
        return {"rows": len(mine), "hits": sum(v[2] - 1 for v in mine)}


class PgGeocodeCache:
    async def get(self, city_id, key):
        async with pool().acquire() as c:
            row = await c.fetchrow(
                """UPDATE geocode_cache SET hits = hits + 1 WHERE city=$1 AND query_norm=$2
                   RETURNING result, updated_at""", city_id, key)
        if row is None:
            return False, None, None
        result = row["result"]
        if isinstance(result, str):
            result = json.loads(result)
        return True, result, dt.datetime.now(dt.UTC) - row["updated_at"]

    async def put(self, city_id, key, result):
        async with pool().acquire() as c:
            await c.execute(
                """INSERT INTO geocode_cache (city, query_norm, result) VALUES ($1, $2, $3::jsonb)
                   ON CONFLICT (city, query_norm) DO UPDATE SET result = EXCLUDED.result, updated_at = now()""",
                city_id, key, json.dumps(result) if result is not None else None)

    async def stats(self, city_id):
        async with pool().acquire() as c:
            row = await c.fetchrow(
                """SELECT count(*) AS rows, coalesce(sum(hits) - count(*), 0) AS hits,
                          count(*) FILTER (WHERE result IS NULL) AS misses
                     FROM geocode_cache WHERE city=$1""", city_id)
        return {"rows": row["rows"], "hits": int(row["hits"]), "misses": row["misses"]}


def cache_fresh(found: bool, result: dict | None, age: dt.timedelta | None) -> bool:
    """A cached hit is good for months (a cadastral point does not move); a cached miss for a week."""
    if not found or age is None:
        return False
    return age <= (CACHE_HIT_TTL if result is not None else CACHE_MISS_TTL)


# ------------------------------------------------------------------ named areas


class PlaceAreaStore(Protocol):
    async def replace(self, city_id: str, kind: str, areas: list[dict]) -> int: ...
    async def assign_parents(self, city_id: str, kind: str, parent_kind: str) -> int: ...
    async def search(self, city_id: str, q: str, limit: int) -> list[dict]: ...
    async def stats(self, city_id: str) -> dict: ...


def _centroid(coords) -> tuple[float, float]:
    """Mean of the outer ring's vertices, (lat, lon) — good enough for a pin and needs no PostGIS."""
    pts = []
    def walk(c):
        if c and isinstance(c[0], (int, float)):
            pts.append(c)
        else:
            for x in c:
                walk(x)
    walk(coords)
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)) if pts else (0.0, 0.0)


def _point_in_polygon(lat: float, lon: float, coords) -> bool:
    """Ray casting over every ring of a (Multi)Polygon's outer rings; holes ignored — a district has none."""
    def ring_hit(ring) -> bool:
        inside = False
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            if (y1 > lat) != (y2 > lat):
                x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                if x > lon:
                    inside = not inside
        return inside
    polys = coords if coords and coords[0] and isinstance(coords[0][0][0], list) else [coords]
    return any(ring_hit(poly[0]) for poly in polys if poly)


class MemoryPlaceAreaStore:
    def __init__(self) -> None:
        self.areas: dict[tuple[str, str], list[dict]] = {}
        self.updated: dict[str, dt.datetime] = {}

    async def replace(self, city_id, kind, areas):
        self.areas[(city_id, kind)] = [dict(a) for a in areas]
        self.updated[city_id] = dt.datetime.now(dt.UTC)
        return len(areas)

    async def assign_parents(self, city_id, kind, parent_kind):
        parents = self.areas.get((city_id, parent_kind)) or []
        n = 0
        for a in self.areas.get((city_id, kind)) or []:
            for p in parents:
                if _point_in_polygon(a["lat"], a["lon"], p["coordinates"]):
                    a["parent_name"] = p["name"]
                    n += 1
                    break
        return n

    async def search(self, city_id, q, limit):
        qn = normalize_name(q)
        out = []
        for (c, kind), areas in self.areas.items():
            if c != city_id:
                continue
            for a in areas:
                nn = a["name_norm"]
                score = 1.0 if nn == qn else 0.9 if nn.startswith(qn) else 0.6 if qn in nn else 0
                if score:
                    out.append((score, kind, a))
        out.sort(key=lambda t: (-t[0], t[1] != "localidad", len(t[2]["name"])))
        return [{"kind": k, **a} for _, k, a in out[:limit]]

    async def stats(self, city_id):
        return {"barrios": len(self.areas.get((city_id, "barrio")) or []),
                "localidades": len(self.areas.get((city_id, "localidad")) or []),
                "updatedAt": self.updated[city_id].isoformat() if city_id in self.updated else None}


class PgPlaceAreaStore:
    async def replace(self, city_id, kind, areas):
        async with pool().acquire() as c, c.transaction():
            await c.execute("DELETE FROM place_area WHERE city=$1 AND kind=$2", city_id, kind)
            await c.executemany(
                """INSERT INTO place_area (city, kind, code, name, name_norm, geom, centroid)
                   VALUES ($1, $2, $3, $4, $5,
                           ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON($6), 4326)),
                           ST_SetSRID(ST_MakePoint($8, $7), 4326))
                   ON CONFLICT (city, kind, code) DO UPDATE
                     SET name = EXCLUDED.name, name_norm = EXCLUDED.name_norm, geom = EXCLUDED.geom,
                         centroid = EXCLUDED.centroid, updated_at = now()""",
                [(city_id, kind, a["code"], a["name"], a["name_norm"],
                  json.dumps({"type": a["geometry_type"], "coordinates": a["coordinates"]}), a["lat"], a["lon"])
                 for a in areas])
        return len(areas)

    async def assign_parents(self, city_id, kind, parent_kind):
        async with pool().acquire() as c:
            res = await c.execute(
                """UPDATE place_area a SET parent_name = p.name
                     FROM place_area p
                    WHERE a.city=$1 AND a.kind=$2 AND p.city=$1 AND p.kind=$3
                      AND ST_Contains(p.geom, a.centroid)""", city_id, kind, parent_kind)
        return int(res.split()[-1]) if res else 0

    async def search(self, city_id, q, limit):
        qn = normalize_name(q)
        if len(qn) < 2:
            return []
        async with pool().acquire() as c:
            rows = await c.fetch(
                """SELECT kind, code, name, name_norm, parent_name,
                          ST_Y(centroid) AS lat, ST_X(centroid) AS lon,
                          GREATEST(similarity(name_norm, $2),
                                   CASE WHEN name_norm = $2 THEN 1.0
                                        WHEN name_norm LIKE $3 THEN 0.9 ELSE 0 END) AS score
                     FROM place_area
                    WHERE city=$1 AND (name_norm % $2 OR name_norm LIKE $3)
                    ORDER BY score DESC, (kind = 'localidad') DESC, length(name)
                    LIMIT $4""", city_id, qn, f"{qn}%", limit)
        return [dict(r) for r in rows]

    async def stats(self, city_id):
        async with pool().acquire() as c:
            rows = await c.fetch(
                "SELECT kind, count(*) AS n, max(updated_at) AS at FROM place_area WHERE city=$1 GROUP BY kind",
                city_id)
        by = {r["kind"]: r for r in rows}
        at = max((r["at"] for r in rows), default=None)
        return {"barrios": by["barrio"]["n"] if "barrio" in by else 0,
                "localidades": by["localidad"]["n"] if "localidad" in by else 0,
                "updatedAt": at.isoformat() if at else None}


def is_arcgis_layer(url: str) -> bool:
    return "/MapServer/" in url or "/FeatureServer/" in url


async def fetch_arcgis_layer(url: str, *, transport: httpx.AsyncBaseTransport | None = None,
                             page: int = 1000) -> list[dict]:
    """Every feature of a layer as GeoJSON. An ArcGIS feature layer (`…/MapServer/37`) is paged with
    `resultOffset` until the server says it is done; any other URL is a plain GeoJSON FeatureCollection —
    the form we publish ourselves when a city's GIS server cannot be reached from where the API runs
    (Catastro's answers a connection from Bogotá and times out one from a US data centre)."""
    feats: list[dict] = []
    offset = 0
    async with httpx.AsyncClient(timeout=settings().ARCGIS_TIMEOUT_S, transport=transport, follow_redirects=True,
                                 headers={"User-Agent": settings().GEOCODER_USER_AGENT}) as cli:
        if not is_arcgis_layer(url):
            r = await cli.get(url)
            r.raise_for_status()
            return list(r.json().get("features") or [])
        while True:
            r = await cli.get(f"{url.rstrip('/')}/query",
                              params={"where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson",
                                      "resultOffset": offset, "resultRecordCount": page})
            r.raise_for_status()
            body = r.json()
            got = body.get("features") or []
            feats.extend(got)
            more = body.get("exceededTransferLimit") or (body.get("properties") or {}).get("exceededTransferLimit")
            if not got or (len(got) < page and not more) or not more:
                break
            offset += len(got)
    return feats


def areas_from_features(feats: list[dict], *, name_field: str, code_field: str) -> list[dict]:
    out = []
    for f in feats:
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        name = str(props.get(name_field) or "").strip()
        if not name or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        lat, lon = _centroid(geom["coordinates"])
        out.append({"code": str(props.get(code_field) or name), "name": name.title(),
                    "name_norm": normalize_name(name), "geometry_type": geom["type"],
                    "coordinates": geom["coordinates"], "lat": lat, "lon": lon})
    return out


async def refresh_place_areas(store: PlaceAreaStore, city: City, cfg: PlaceAreas | None = None, *,
                              transport: httpx.AsyncBaseTransport | None = None) -> dict:
    cfg = cfg or city.geocoder.areas
    if not cfg.active:
        return {"barrios": 0, "localidades": 0}
    result: dict = {}
    if cfg.localidades_url:
        feats = await fetch_arcgis_layer(cfg.localidades_url, transport=transport)
        areas = areas_from_features(feats, name_field=cfg.localidades_name_field, code_field=cfg.localidades_code_field)
        result["localidades"] = await store.replace(city.id, "localidad", areas)
    feats = await fetch_arcgis_layer(cfg.barrios_url, transport=transport)
    areas = areas_from_features(feats, name_field=cfg.barrios_name_field, code_field=cfg.barrios_code_field)
    result["barrios"] = await store.replace(city.id, "barrio", areas)
    if cfg.localidades_url:
        result["withParent"] = await store.assign_parents(city.id, "barrio", "localidad")
    return result


def area_result(row: dict, locale: str | None) -> dict:
    kind = row["kind"]
    label = area_noun(kind, locale)
    if row.get("parent_name"):
        label += f" · {row['parent_name']}"
    return {"id": f"area:{kind}:{row['code']}", "name": row["name"], "label": label,
            "lat": float(row["lat"]), "lon": float(row["lon"]), "type": "place", "stopId": None,
            "component": None, "source": "catastro"}


async def search_areas(store: PlaceAreaStore, city: City, q: str, limit: int, locale: str | None) -> list[dict]:
    if not city.geocoder.areas.active:
        return []
    try:
        rows = await store.search(city.id, q, limit)
    except Exception as e:  # noqa: BLE001
        log.error("[%s] area search failed: %s", city.id, e)
        return []
    return [area_result(r, locale or city.locale) for r in rows]
