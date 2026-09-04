"""Shared-vehicle (GBFS) networks and stations, one or more per city. Served from the in-memory GBFS cache."""
import datetime as dt

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..errors import ApiError
from ..models import RentalNetworksResponse, RentalStationDetail, RentalStationsResponse
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["rental"])


class RentalStationNotFound(ApiError):
    status, code = 404, "RENTAL_STATION_NOT_FOUND"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@router.get("/v1/cities/{city}/rental/networks", response_model=RentalNetworksResponse)
async def rental_networks(rt: CityRuntime = Depends(city_runtime)):
    for n in rt.gbfs.values():
        await n.refresh()
    body = {"networks": [n.summary() for n in rt.gbfs.values()]}
    return JSONResponse(RentalNetworksResponse.model_validate(body).model_dump(by_alias=True),
                        headers={"Cache-Control": "public, max-age=300"})


@router.get("/v1/cities/{city}/rental/stations", response_model=RentalStationsResponse)
async def rental_stations(rt: CityRuntime = Depends(city_runtime),
                          bbox: str | None = Query(None, pattern=r"^-?[\d.]+(,-?[\d.]+){3}$"),
                          networkId: str | None = None, limit: int = Query(500, ge=1, le=2000)):
    box = tuple(float(t) for t in bbox.split(",")) if bbox else None
    nets = [n for n in rt.gbfs.values() if not networkId or n.cfg.id == networkId]
    stations: list[dict] = []
    ttl = 30
    for n in nets:
        await n.refresh()
        ttl = min(ttl, n.ttl) if stations else n.ttl
        stations.extend(n.stations(box, limit - len(stations)))
        if len(stations) >= limit:
            break
    body = {"generatedAt": _now(), "ttlSeconds": ttl, "stations": stations}
    return JSONResponse(RentalStationsResponse.model_validate(body).model_dump(by_alias=True),
                        headers={"Cache-Control": f"public, max-age={ttl}"})


@router.get("/v1/cities/{city}/rental/stations/{stationId}", response_model=RentalStationDetail)
async def rental_station(stationId: str, rt: CityRuntime = Depends(city_runtime)):
    net_id = stationId.split(":", 1)[0] if ":" in stationId else None
    for n in rt.gbfs.values():
        if net_id and n.cfg.id != net_id:
            continue
        await n.refresh()
        s = n.station(stationId, detail=True)
        if s:
            return JSONResponse(RentalStationDetail.model_validate(s).model_dump(by_alias=True),
                                headers={"Cache-Control": f"public, max-age={n.ttl}"})
    raise RentalStationNotFound(f"rental station '{stationId}' not found")
