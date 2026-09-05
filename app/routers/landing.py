"""White-label city landing page: the (admin-editable) landing config plus live stats, in one public call."""
import datetime as dt
import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..db import pool
from ..errors import ApiError
from ..models import LandingResponse
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["platform"])
STATS_TTL = 60          # seconds the computed stats are reused
CACHE_MAX_AGE = 300     # HTTP cache for the whole page payload


class LandingDisabled(ApiError):
    status, code = 404, "LANDING_DISABLED"


async def _static_counts(rt: CityRuntime) -> tuple[int | None, int | None]:
    """Routes/stops of the active feed; None when the database is not reachable (tests, cold start)."""
    try:
        async with pool().acquire() as c:
            row = await c.fetchrow("SELECT n_routes, n_stops FROM feed_version WHERE city=$1 AND is_active LIMIT 1",
                                   rt.city.id)
    except Exception:  # noqa: BLE001 - stats are decorative; the page must render without them
        return None, None
    return (row["n_routes"], row["n_stops"]) if row else (None, None)


async def landing_stats(rt: CityRuntime) -> dict:
    cached = rt.meta.get("landing_stats")
    if cached and time.time() - cached["_at"] < STATS_TTL:
        return {k: v for k, v in cached.items() if k != "_at"}
    routes, stops = await _static_counts(rt)
    stats = {
        "routes": routes, "stops": stops,
        "vehiclesLive": len(rt.rt.vehicles) if not rt.freshness()["stale"] else 0,
        "bikeStations": sum(len(g.station_info) for g in rt.gbfs.values()) or None,
        "alertsActive": len(rt.rt.active_alerts()),
        "generatedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    rt.meta["landing_stats"] = {**stats, "_at": time.time()}
    return stats


@router.get("/v1/cities/{city}/landing", response_model=LandingResponse)
async def city_landing(rt: CityRuntime = Depends(city_runtime)):
    city = rt.city
    if not city.landing.enabled:
        raise LandingDisabled(f"landing page is disabled for '{city.id}'")
    pub = city.public()
    body = {
        "city": {k: pub[k] for k in ("id", "name", "country", "locale", "branding", "attribution", "links",
                                     "services")},
        "landing": city.landing_public(),
        "stats": await landing_stats(rt),
        "apps": city.landing.apps.model_dump(by_alias=True),
    }
    body["city"]["features"] = pub["features"]
    body["city"]["mobility"] = {
        "bikeShare": [{"id": n.id, "name": n.name, "color": n.color, "url": n.url}
                      for n in city.mobility.bike_share],
        # public shape only (no templates or credentials): enough for the landing's "taxi y apps" highlight
        "onDemand": [{"id": p.id, "name": p.name, "kind": p.kind, "color": p.color}
                     for p in city.on_demand_providers()],
    }
    return JSONResponse(LandingResponse.model_validate(body).model_dump(by_alias=True),
                        headers={"Cache-Control": f"public, max-age={CACHE_MAX_AGE}"})
