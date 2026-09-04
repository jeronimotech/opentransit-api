import hmac

from fastapi import APIRouter, Depends, Header

from ..config import settings
from ..errors import Unauthorized
from ..gtfs_static import ingest, load_route_index
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["admin"])


def require_admin(x_admin_token: str | None = Header(None)) -> None:
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings().ADMIN_TOKEN):
        raise Unauthorized("missing or invalid X-Admin-Token")


@router.post("/v1/admin/cities/{city}/ingest-static", dependencies=[Depends(require_admin)])
async def ingest_static(rt: CityRuntime = Depends(city_runtime), force: bool = False):
    result = await ingest(rt.city, force=force)
    rt.rt.set_static(*await load_route_index(rt.city))
    rt.static_ready = True
    return result


@router.post("/v1/admin/cities/{city}/purge", dependencies=[Depends(require_admin)])
async def purge(rt: CityRuntime = Depends(city_runtime)):
    """Drop in-memory vehicle history for this city (the only per-city state that grows)."""
    n = len(rt.rt.history)
    rt.rt.history.clear()
    return {"purgedVehicles": n}
