from fastapi import APIRouter, Depends, Query

from .. import geocode as geo
from ..models import GeocodeResponse, ReverseResponse
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["search"])


@router.get("/v1/cities/{city}/geocode", response_model=GeocodeResponse)
async def geocode(rt: CityRuntime = Depends(city_runtime), q: str = Query(..., min_length=1, max_length=120),
                  lat: float | None = None, lon: float | None = None, limit: int = Query(8, ge=1, le=25)):
    return {"results": await geo.geocode(rt.city, q, lat, lon, limit)}


@router.get("/v1/cities/{city}/reverse", response_model=ReverseResponse)
async def reverse(rt: CityRuntime = Depends(city_runtime), lat: float = Query(...), lon: float = Query(...)):
    return await geo.reverse(rt.city, lat, lon)
