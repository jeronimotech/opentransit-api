"""On-demand mobility (v1.4): providers, price estimates for a car ride, and provider hand-off links."""
import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..errors import ApiError
from ..models import OnDemandEstimateResponse, OnDemandHandoffResponse, OnDemandProvidersResponse
from ..ondemand import build_handoff, quotes_for
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["ondemand"])


class OnDemandDisabled(ApiError):
    status, code = 404, "ONDEMAND_DISABLED"


class ProviderNotFound(ApiError):
    status, code = 404, "PROVIDER_NOT_FOUND"


class NoCarRoute(ApiError):
    status, code = 404, "NO_ROUTE"


class HandoffUnavailable(ApiError):
    status, code = 404, "HANDOFF_UNAVAILABLE"


def parse_when(rt: CityRuntime, time: str | None) -> dt.datetime:
    tz = ZoneInfo(rt.city.timezone)
    if not time:
        return dt.datetime.now(tz)
    try:
        when = dt.datetime.fromisoformat(time.replace("Z", "+00:00"))
    except ValueError as e:
        raise ApiError(f"time: {e}") from e
    return when.replace(tzinfo=tz) if when.tzinfo is None else when.astimezone(tz)


def _require(rt: CityRuntime) -> None:
    if not rt.city.on_demand_enabled():
        raise OnDemandDisabled(f"on-demand mobility is not configured for {rt.city.name}")


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/v1/cities/{city}/ondemand/providers", response_model=OnDemandProvidersResponse)
async def providers(rt: CityRuntime = Depends(city_runtime)):
    """Enabled providers in display order. Credentials and hand-off templates never appear here."""
    _require(rt)
    city = rt.city
    body = {"providers": [p.public() for p in city.on_demand_providers()],
            "policy": city.mobility.on_demand_policy.public(),
            "tariffs": [t.public() for t in city.mobility.taxi_tariffs]}
    return JSONResponse(OnDemandProvidersResponse.model_validate(body).model_dump(by_alias=True),
                        headers={"Cache-Control": "public, max-age=300"})


@router.get("/v1/cities/{city}/ondemand/estimate", response_model=OnDemandEstimateResponse)
async def estimate(request: Request, rt: CityRuntime = Depends(city_runtime),
                   fromLat: float = Query(..., ge=-90, le=90), fromLon: float = Query(..., ge=-180, le=180),
                   toLat: float = Query(..., ge=-90, le=90), toLon: float = Query(..., ge=-180, le=180),
                   time: str | None = Query(None, description="ISO-8601 departure; default now (city timezone)"),
                   providerId: str | None = None,
                   options: str | None = Query(None, description="comma list of optional surcharge ids, e.g. door"),
                   fromName: str | None = Query(None, max_length=120), toName: str | None = Query(None, max_length=120),
                   locale: str = Query("es", pattern="^(es|en)$")):
    """Car route (OTP) + one quote per provider: taxi tariff estimate, or "price in the app"."""
    _require(rt)
    city = rt.city
    ids = {providerId} if providerId else None
    if ids and not city.on_demand_provider(providerId):
        raise ProviderNotFound(f"unknown provider '{providerId}'")
    when = parse_when(rt, time)
    route = await rt.car_router().route(fromLat, fromLon, toLat, toLon, when)
    if not route:
        raise NoCarRoute("no car route found between these points")
    optional = {t.strip() for t in (options or "").split(",") if t.strip()} or None
    quotes = quotes_for(city, distance_m=route["distanceMeters"], duration_s=route["durationSeconds"], when=when,
                        from_lat=fromLat, from_lon=fromLon, to_lat=toLat, to_lon=toLon, from_name=fromName,
                        to_name=toName, base_url=_base_url(request), provider_ids=ids, optional_ids=optional,
                        locale=locale)
    body = {"route": route, "when": when.isoformat(), "estimates": quotes}
    return JSONResponse(OnDemandEstimateResponse.model_validate(body).model_dump(by_alias=True),
                        headers={"Cache-Control": "public, max-age=30"})


@router.get("/v1/cities/{city}/ondemand/handoff", response_model=OnDemandHandoffResponse)
async def handoff(rt: CityRuntime = Depends(city_runtime), providerId: str = Query(...),
                  fromLat: float = Query(..., ge=-90, le=90), fromLon: float = Query(..., ge=-180, le=180),
                  toLat: float = Query(..., ge=-90, le=90), toLon: float = Query(..., ge=-180, le=180),
                  fromName: str | None = Query(None, max_length=120), toName: str | None = Query(None, max_length=120),
                  platform: str | None = Query(None, pattern="^(ios|android|web)$"),
                  redirect: bool = False):
    """Builds the provider's link for this trip server-side (credentials injected here, never sent to clients).
    `redirect=1` answers with a 302 to the link (or its fallback) so a plain anchor works."""
    _require(rt)
    p = rt.city.on_demand_provider(providerId)
    if not p or not p.enabled:
        raise ProviderNotFound(f"unknown provider '{providerId}'")
    built = build_handoff(p, from_lat=fromLat, from_lon=fromLon, to_lat=toLat, to_lon=toLon,
                          from_name=fromName, to_name=toName, platform=platform)
    target = built["url"] or built["fallback"]
    if redirect:
        if not target:
            raise HandoffUnavailable(f"{p.name} has no link configured")
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})
    body = {"url": built["url"], "fallback": built["fallback"], "kind": built["kind"], "provider": p.public(),
            "missingCredentials": built.get("missingCredentials", [])}
    return JSONResponse(OnDemandHandoffResponse.model_validate(body).model_dump(by_alias=True),
                        headers={"Cache-Control": "no-store"})
