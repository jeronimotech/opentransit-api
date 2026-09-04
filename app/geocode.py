"""Search: GTFS stops/stations from Postgres (trigram + prefix), merged with Photon (OSM) results."""
import logging

import httpx

from .cities import City
from .config import settings
from .db import pool
from .geo import haversine_m
from .gtfs_static import normalize_name
from .normalize import stop_from_db

log = logging.getLogger("ot.geocode")


NEARBY_M = 800


def rank_results(results: list[dict], q: str, lat: float | None = None, lon: float | None = None) -> list[dict]:
    """With a user position: GTFS stops/stations within NEARBY_M first (closest first), then stations, then
    other GTFS matches (exact/prefix/word, busier first), then Photon. Without one: stations first."""
    qn = normalize_name(q)
    have_pos = lat is not None and lon is not None

    def dist(r: dict) -> float | None:
        if not have_pos or r.get("lat") is None:
            return None
        return haversine_m(lat, lon, r["lat"], r["lon"])

    def key(r: dict):
        name = normalize_name(r["name"])
        exact = name == qn
        prefix = name.startswith(qn)
        word = any(w.startswith(qn) for w in name.split())
        d = dist(r)
        near = r["source"] == "gtfs" and d is not None and d <= NEARBY_M
        r["distanceMeters"] = int(round(d)) if d is not None else None
        return (
            0 if near else 1 if r["type"] == "station" else 2 if r["source"] == "gtfs" else 3,
            d if near else 0,
            0 if exact else 1 if prefix else 2 if word else 3,
            -(r.get("_nRoutes") or 0),
            len(name),
        )

    return sorted(results, key=key)


async def search_stops(city: City, q: str, lat: float | None, lon: float | None, limit: int) -> list[dict]:
    qn = normalize_name(q)
    if len(qn) < 2:
        return []
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", city.id)
        if not fv:
            return []
        rows = await c.fetch(
            """SELECT stop_id, stop_code, name, lat, lon, location_type, parent_station, wheelchair, component,
                      n_routes,
                      GREATEST(similarity(name_norm, $2), CASE WHEN name_norm LIKE $3 THEN 0.9 ELSE 0 END,
                               CASE WHEN stop_code = $4 THEN 1.0 ELSE 0 END) AS score
                 FROM stop
                WHERE feed_version_id = $1
                  AND (name_norm % $2 OR name_norm LIKE $3 OR stop_code = $4)
                ORDER BY (location_type = 1) DESC, score DESC, n_routes DESC
                LIMIT $5""",
            fv, qn, f"%{qn}%", q.strip(), limit * 3)
    out = []
    for r in rows:
        s = stop_from_db(city, dict(r))
        out.append({
            "id": f"stop:{s['id']}", "name": s["name"],
            "label": ("Estación" if s["locationType"] == "station" else "Parada")
            + (f" · {s['code']}" if s.get("code") else "") + (f" · {s['component']}" if s.get("component") else ""),
            "lat": s["lat"], "lon": s["lon"], "type": s["locationType"] if s["locationType"] != "entrance" else "stop",
            "stopId": s["id"], "component": s.get("component"), "source": "gtfs", "_nRoutes": r["n_routes"],
        })
    return out


def _photon_type(props: dict) -> str:
    kind = (props.get("osm_key") or "")
    if kind in ("highway",):
        return "street"
    if kind in ("building", "place") and props.get("housenumber"):
        return "address"
    if kind in ("amenity", "shop", "tourism", "leisure", "office", "public_transport", "railway", "aeroway"):
        return "poi"
    return "place"


def _photon_label(p: dict) -> str:
    parts = [p.get("street"), p.get("housenumber"), p.get("district") or p.get("locality"), p.get("city")]
    return ", ".join(str(x) for x in parts if x)


async def search_photon(city: City, q: str, lat: float | None, lon: float | None, limit: int) -> list[dict]:
    url = city.geocoder.photon_url
    if not url:
        return []
    params: dict = {"q": q, "limit": limit, "bbox": ",".join(str(x) for x in city.bbox)}
    if lat is not None and lon is not None:
        params.update(lat=lat, lon=lon)
    try:
        async with httpx.AsyncClient(timeout=settings().PHOTON_TIMEOUT_S) as cli:
            r = await cli.get(f"{url.rstrip('/')}/api/", params=params)
            r.raise_for_status()
            feats = r.json().get("features") or []
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] photon failed: %s", city.id, e)
        return []
    out = []
    for f in feats:
        p = f.get("properties") or {}
        c = (f.get("geometry") or {}).get("coordinates") or [None, None]
        name = p.get("name") or p.get("street") or "?"
        out.append({"id": f"photon:{p.get('osm_type', '')}{p.get('osm_id', '')}", "name": name,
                    "label": _photon_label(p) or None, "lat": c[1], "lon": c[0], "type": _photon_type(p),
                    "stopId": None, "component": None, "source": "photon"})
    return out


async def geocode(city: City, q: str, lat: float | None, lon: float | None, limit: int) -> list[dict]:
    import asyncio
    stops, photon = await asyncio.gather(search_stops(city, q, lat, lon, limit),
                                         search_photon(city, q, lat, lon, limit))
    seen, merged = set(), []
    for r in rank_results(stops + photon, q, lat, lon):
        k = (round(r["lat"] or 0, 4), round(r["lon"] or 0, 4), normalize_name(r["name"]))
        if k in seen:
            continue
        seen.add(k)
        r.pop("_nRoutes", None)
        merged.append(r)
    return merged[:limit]


async def reverse(city: City, lat: float, lon: float) -> dict:
    url = city.geocoder.photon_url
    if url:
        try:
            async with httpx.AsyncClient(timeout=settings().PHOTON_TIMEOUT_S) as cli:
                r = await cli.get(f"{url.rstrip('/')}/reverse", params={"lat": lat, "lon": lon})
                r.raise_for_status()
                feats = r.json().get("features") or []
            if feats:
                p = feats[0]["properties"]
                name = ", ".join(str(x) for x in (p.get("name"), p.get("street"), p.get("housenumber")) if x)
                return {"name": name or _photon_label(p) or f"{lat:.5f}, {lon:.5f}", "lat": lat, "lon": lon}
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] photon reverse failed: %s", city.id, e)
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", city.id)
        row = await c.fetchrow(
            """SELECT name FROM stop WHERE feed_version_id=$1
               ORDER BY geog <-> ST_SetSRID(ST_MakePoint($3,$2),4326)::geography LIMIT 1""",
            fv, lat, lon) if fv else None
    return {"name": (row["name"] if row else None) or f"{lat:.5f}, {lon:.5f}", "lat": lat, "lon": lon}
