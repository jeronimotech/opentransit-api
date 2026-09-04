"""Station services layer: bike parking, toilets, ATMs, health points, libraries... from OSM (Overpass),
pre-built per city into `cities/<slug>/pois.geojson` by scripts/build-pois.sh. Served with bbox/type filters."""
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..config import settings
from ..runtime import CityRuntime, city_runtime

log = logging.getLogger("ot.pois")
router = APIRouter(tags=["stops"])
POI_TYPES = ("bike_parking", "toilets", "atm", "health", "library", "police", "pharmacy")


def pois_path(city) -> Path:
    return Path(settings().CITIES_DIR) / (city.pois_file or f"{city.id}/pois.geojson")


def load_pois(rt: CityRuntime) -> list[dict]:
    if "pois" not in rt.meta:
        p = pois_path(rt.city)
        feats: list[dict] = []
        if p.exists():
            try:
                feats = [f for f in (json.loads(p.read_text(encoding="utf-8")).get("features") or []) if f]
            except ValueError:
                log.warning("[%s] invalid pois file %s", rt.city.id, p)
        rt.meta["pois"] = feats
    return rt.meta["pois"]


def filter_pois(feats: list[dict], bbox: tuple[float, float, float, float] | None,
                types: set[str] | None) -> list[dict]:
    out = []
    for f in feats:
        props = f.get("properties") or {}
        if types and props.get("type") not in types:
            continue
        if bbox:
            coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
            lon, lat = coords[0], coords[1]
            if lon is None or not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
                continue
        out.append(f)
    return out


@router.get("/v1/cities/{city}/pois")
async def pois(rt: CityRuntime = Depends(city_runtime),
               bbox: str | None = Query(None, pattern=r"^-?[\d.]+(,-?[\d.]+){3}$"),
               type: str | None = Query(None, description="comma list: " + ",".join(POI_TYPES)),
               limit: int = Query(2000, ge=1, le=10000)):
    feats = load_pois(rt)
    box = tuple(float(t) for t in bbox.split(",")) if bbox else None
    types = {t.strip() for t in type.split(",") if t.strip()} if type else None
    sel = filter_pois(feats, box, types)[:limit]
    return JSONResponse({"type": "FeatureCollection", "features": sel,
                         "meta": {"count": len(sel), "total": len(feats), "types": list(POI_TYPES)}},
                        headers={"Cache-Control": "public, max-age=3600"})
