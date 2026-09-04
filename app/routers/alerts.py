from fastapi import APIRouter, Depends

from ..models import AlertsResponse
from ..runtime import CityRuntime, city_runtime
from .vehicles import _public_alert

router = APIRouter(tags=["realtime"])


@router.get("/v1/cities/{city}/alerts", response_model=AlertsResponse)
async def alerts(rt: CityRuntime = Depends(city_runtime), routeId: str | None = None, stopId: str | None = None,
                 active: bool = True):
    cache, city = rt.rt, rt.city
    items = cache.active_alerts() if active else cache.alerts
    if routeId:
        raw = city.unscoped(routeId)
        items = [a for a in items if raw in a["routeIds"]]
    if stopId:
        raw = city.unscoped(stopId)
        items = [a for a in items if raw in a["stopIds"]]
    return {"alerts": [_public_alert(cache, city, a) for a in items]}
