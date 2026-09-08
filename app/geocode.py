"""Search: GTFS stops/stations from Postgres (trigram + prefix), merged with Photon (OSM) results."""
import logging
import re
import time

import httpx

from .cities import City
from .config import settings
from .db import pool
from .geo import haversine_m
from .gtfs_static import normalize_name
from .normalize import stop_from_db

log = logging.getLogger("ot.geocode")


NEARBY_M = 800

# The one word the API puts in a result's label. Everything else in `label` is data
# (a stop code, a component), but this is UI text, and it used to be Spanish for every
# city — so an English reader saw "Estación" and a city that does not speak Spanish
# would have got Spanish nouns for its own stops.
STOP_NOUNS = {
    "es": {"station": "Estación", "stop": "Parada"},
    "en": {"station": "Station", "stop": "Stop"},
}


def stop_noun(location_type: str, locale: str | None) -> str:
    lang = (locale or "es").split("-")[0].lower()
    words = STOP_NOUNS.get(lang) or STOP_NOUNS["es"]
    return words["station"] if location_type == "station" else words["stop"]

# Reserve part of the page for address/POI results. In a stop-dense city every GTFS
# station outranks every Photon hit, so a pure sort + truncate returns eight unrelated
# stations for "Calle 85 #12-30" and never the address itself.
MIN_PLACE_SLOTS = 3

# A house number is the strongest signal that the user means a street address and not a
# similarly named stop. Covers "Calle 85 #12-30", "Cra 7 No 72-41", "Av 68 # 24 - 10" and
# the anglophone "221B Baker Street".
_HOUSE_NUMBER = re.compile(r"(#\s*\d|\bn[o°º]?\.?\s*\d|\d+\s*-\s*\d|^\s*\d+[a-z]?\s+\w)", re.I)


def looks_like_address(q: str) -> bool:
    return bool(_HOUSE_NUMBER.search(q or ""))


def _geocoder_headers() -> dict:
    return {"User-Agent": settings().GEOCODER_USER_AGENT}


class _ProviderHealth:
    """Photon failing is invisible otherwise: the caller still gets GTFS stops and no error.
    It was returning 403 for every request and nothing surfaced it."""

    def __init__(self) -> None:
        self.ok = 0
        self.failed = 0
        self.last_error: str | None = None
        self.last_error_at: float | None = None

    def record_ok(self) -> None:
        self.ok += 1

    def record_failure(self, e: object) -> None:
        self.failed += 1
        self.last_error = str(e)[:300]
        self.last_error_at = time.time()

    def snapshot(self) -> dict:
        total = self.ok + self.failed
        return {"calls": total, "failed": self.failed,
                "okRate": round(self.ok / total, 3) if total else None,
                "lastError": self.last_error,
                "lastErrorAgeSeconds": int(time.time() - self.last_error_at) if self.last_error_at else None}


photon_health = _ProviderHealth()


def rank_results(results: list[dict], q: str, lat: float | None = None, lon: float | None = None) -> list[dict]:
    """With a user position: GTFS stops/stations within NEARBY_M first (closest first), then stations, then
    other GTFS matches (exact/prefix/word, busier first), then Photon. Without one: stations first."""
    qn = normalize_name(q)
    have_pos = lat is not None and lon is not None
    named_query = " " in qn.strip()
    # "Calle 85 #12-30" is not a request for the station named "Calle 34". When the query
    # carries a house number the address IS the answer, so it outranks every stop.
    address_query = looks_like_address(q)

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
        # A one-word query is a category search ("portal", "calle") where the
        # station is the useful answer. A multi-word query is a name search, and
        # there an exact match IS the answer: "Parque de la 93" used to return
        # the station "Parque" first, and the assistant planned from the wrong
        # place because of it.
        addr_hit = address_query and r["source"] == "photon" and r["type"] in ("address", "street")
        return (
            0 if addr_hit else
            1 if near else 2 if (exact and named_query) else 3 if r["type"] == "station"
            else 4 if r["source"] == "gtfs" else 5,
            d if near else 0,
            0 if exact else 1 if prefix else 2 if word else 3,
            -(r.get("_nRoutes") or 0),
            len(name),
        )

    return sorted(results, key=key)


async def search_stops(city: City, q: str, lat: float | None, lon: float | None, limit: int,
                       locale: str | None = None) -> list[dict]:
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
            "label": stop_noun(s["locationType"], locale or city.locale)
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
        async with httpx.AsyncClient(timeout=settings().PHOTON_TIMEOUT_S,
                                     headers=_geocoder_headers()) as cli:
            r = await cli.get(f"{url.rstrip('/')}/api/", params=params)
            r.raise_for_status()
            feats = r.json().get("features") or []
        photon_health.record_ok()
    except Exception as e:  # noqa: BLE001
        photon_health.record_failure(e)
        # Errors here silently degrade search to stops-only, so they are not a warning.
        log.error("[%s] photon search failed (addresses unavailable): %s", city.id, e)
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


async def geocode(city: City, q: str, lat: float | None, lon: float | None, limit: int,
                  locale: str | None = None) -> list[dict]:
    import asyncio
    # Over-fetch from both sources: ranking and street collapsing need candidates to choose
    # from. Asking Photon for exactly `limit` once returned the Calle 85 segment 6 km from
    # the user because the nearer one never made it into the response.
    stops, photon = await asyncio.gather(search_stops(city, q, lat, lon, limit, locale),
                                         search_photon(city, q, lat, lon, max(limit * 3, 15)))
    seen, merged = set(), []
    for r in rank_results(_collapse_streets(city, photon, lat, lon) + stops, q, lat, lon):
        k = (round(r["lat"] or 0, 4), round(r["lon"] or 0, 4), normalize_name(r["name"]))
        if k in seen:
            continue
        seen.add(k)
        r.pop("_nRoutes", None)
        merged.append(r)
    return _reserve_place_slots(merged, limit)


def _collapse_streets(city: City, results: list[dict], lat: float | None, lon: float | None) -> list[dict]:
    """OSM splits a long street into segments, so "Calle 85" comes back several times.
    Keep one per name, and keep the segment nearest the user (city centre when we have no
    position) -- picking an arbitrary segment can land the trip kilometres from the door."""
    ref_lat = lat if lat is not None else city.center.lat
    ref_lon = lon if lon is not None else city.center.lon
    best: dict[str, dict] = {}
    out = []
    for r in results:
        if r["type"] != "street" or r.get("lat") is None:
            out.append(r)
            continue
        key = normalize_name(r["name"])
        d = haversine_m(ref_lat, ref_lon, r["lat"], r["lon"])
        if key not in best or d < best[key]["_d"]:
            best[key] = {**r, "_d": d}
    for r in best.values():
        r.pop("_d", None)
        out.append(r)
    return out


def _reserve_place_slots(ranked: list[dict], limit: int) -> list[dict]:
    """Keep the ranked order but guarantee addresses and POIs a share of the page.

    Ranking alone is not enough: a query like "Calle 85" matches dozens of GTFS stations,
    all of which sort above any Photon result, so plain truncation returns stops only and
    the user can never pick an address to walk or cycle to."""
    head = ranked[:limit]
    quota = min(MIN_PLACE_SLOTS, limit)
    places_in_head = sum(1 for r in head if r["source"] != "gtfs")
    missing = quota - places_in_head
    if missing <= 0:
        return head
    spare = [r for r in ranked[limit:] if r["source"] != "gtfs"][:missing]
    if not spare:
        return head
    # Drop the weakest GTFS rows to make room, never a place already earned its slot.
    droppable = [i for i, r in enumerate(head) if r["source"] == "gtfs"]
    for i in droppable[-len(spare):]:
        head[i] = None  # type: ignore[call-overload]
    kept = [r for r in head if r is not None]
    order = {id(r): i for i, r in enumerate(ranked)}
    return sorted(kept + spare, key=lambda r: order[id(r)])


async def reverse(city: City, lat: float, lon: float) -> dict:
    url = city.geocoder.photon_url
    if url:
        try:
            async with httpx.AsyncClient(timeout=settings().PHOTON_TIMEOUT_S,
                                         headers=_geocoder_headers()) as cli:
                r = await cli.get(f"{url.rstrip('/')}/reverse", params={"lat": lat, "lon": lon})
                r.raise_for_status()
                feats = r.json().get("features") or []
            photon_health.record_ok()
            if feats:
                p = feats[0]["properties"]
                name = ", ".join(str(x) for x in (p.get("name"), p.get("street"), p.get("housenumber")) if x)
                return {"name": name or _photon_label(p) or f"{lat:.5f}, {lon:.5f}", "lat": lat, "lon": lon}
        except Exception as e:  # noqa: BLE001
            photon_health.record_failure(e)
            log.error("[%s] photon reverse failed: %s", city.id, e)
    async with pool().acquire() as c:
        fv = await c.fetchval("SELECT id FROM feed_version WHERE city=$1 AND is_active LIMIT 1", city.id)
        row = await c.fetchrow(
            """SELECT name FROM stop WHERE feed_version_id=$1
               ORDER BY geog <-> ST_SetSRID(ST_MakePoint($3,$2),4326)::geography LIMIT 1""",
            fv, lat, lon) if fv else None
    return {"name": (row["name"] if row else None) or f"{lat:.5f}, {lon:.5f}", "lat": lat, "lon": lon}
