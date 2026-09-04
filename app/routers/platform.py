from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..models import Healthz
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["platform"])


@router.get("/healthz", response_model=Healthz)
async def healthz(request: Request):
    return {"status": "ok", "version": __version__, "cities": sorted(request.app.state.cities)}


@router.get("/v1/cities")
async def list_cities(request: Request):
    cities = [r.city.public() for r in request.app.state.cities.values()]
    return JSONResponse({"cities": cities}, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/v1/cities/{city}")
async def get_city(rt: CityRuntime = Depends(city_runtime)):
    return JSONResponse(rt.city.public(), headers={"Cache-Control": "public, max-age=3600"})
