from fastapi import APIRouter, Depends, Request

from ..db import pool
from ..models import CityHealth
from ..rt import iso
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["platform"])


@router.get("/v1/cities/{city}/health", response_model=CityHealth)
async def city_health(request: Request, rt: CityRuntime = Depends(city_runtime)):
    async with pool().acquire() as c:
        fv = await c.fetchrow("SELECT id, fetched_at, n_routes, n_stops, n_trips, feed_info FROM feed_version "
                              "WHERE city=$1 AND is_active LIMIT 1", rt.city.id)
    info = await rt.otp.server_info()
    cache = rt.rt
    f = rt.city.feeds
    return {
        "static": {"feedVersion": str(fv["id"]) if fv else None,
                   "fetchedAt": fv["fetched_at"].isoformat() if fv else None,
                   "routes": fv["n_routes"] if fv else None, "stops": fv["n_stops"] if fv else None,
                   "trips": fv["n_trips"] if fv else None},
        "realtime": {"enabled": bool(f.rt_positions_url or f.rt_tripupdates_url or f.rt_alerts_url),
                     "lastFetchAt": iso(cache.updated_at), **{k: v for k, v in cache.health().items()
                                                                 if k != "pctTripResolved"},
                     "vehicles": len(cache.vehicles), "pctTripResolved": cache.health()["pctTripResolved"],
                     "alerts": len(cache.active_alerts()),
                     "stale": rt.freshness()["stale"], "staleSeconds": rt.freshness()["staleSeconds"]},
        "router": {"up": info is not None, "version": rt.otp.version,
                   "graphBuiltAt": (info or {}).get("transitTimeZone") and None or _built_at(info),
                   "baseUrl": rt.city.otp.base_url},
        "rental": {"networks": [g.health() for g in rt.gbfs.values()]},
        "ondemand": {"providers": len(rt.city.on_demand_providers()), "tariffs": len(rt.city.mobility.taxi_tariffs),
                     "routerCar": (await rt.car_router().probe(rt.city)) if rt.city.on_demand_providers() else None},
        "analytics": {"enabled": rt.city.config.analytics.enabled, **(await _analytics_health(request, rt))},
    }


async def _analytics_health(request: Request, rt: CityRuntime) -> dict:
    store = getattr(request.app.state, "analytics_store", None)
    if store is None:
        return {}
    try:
        return await store.health(rt.city.id)
    except Exception:  # noqa: BLE001
        return {}


def _built_at(info: dict | None) -> str | None:
    if not info:
        return None
    for k in ("buildTime", "graphBuildTime", "builtAt"):
        if info.get(k):
            return str(info[k])
    v = info.get("version") or {}
    return v.get("buildTime") if isinstance(v, dict) else None
