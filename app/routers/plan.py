import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query

from ..errors import ApiError
from ..models import PlanResponse
from ..normalize import plan_from_otp
from ..otp import PLAN_QUERY
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["planning"])

STREET = {"WALK", "BICYCLE", "CAR", "SCOOTER"}
TRANSIT = {"BUS", "RAIL", "SUBWAY", "TRAM", "CABLE_CAR", "FERRY", "GONDOLA", "FUNICULAR", "TROLLEYBUS", "MONORAIL"}


def parse_modes(raw: str | None, city_transit: list[str]) -> tuple[list[str], list[str]]:
    """'TRANSIT,WALK' -> (transit modes, street modes). Unknown tokens are rejected."""
    if not raw:
        return list(city_transit), ["WALK"]
    transit: list[str] = []
    street: list[str] = []
    for tok in (t.strip().upper() for t in raw.split(",") if t.strip()):
        if tok == "TRANSIT":
            transit.extend(m for m in city_transit if m not in transit)
        elif tok in TRANSIT:
            if tok not in transit:
                transit.append(tok)
        elif tok in STREET:
            if tok not in street:
                street.append(tok)
        else:
            raise ApiError(f"unknown mode '{tok}'")
    if not street:
        street = ["WALK"]
    return transit, street


def build_variables(*, from_lat: float, from_lon: float, to_lat: float, to_lon: float, when: dt.datetime,
                    arrive_by: bool, transit: list[str], street: list[str], wheelchair: bool,
                    num: int, locale: str, walk_reluctance: float | None) -> dict:
    modes: dict = {}
    if transit:
        # Access/egress stay on foot: feeds rarely declare bikes_allowed, and OTP then finds nothing.
        # A requested BICYCLE is offered as a direct (bike-only) alternative next to the transit options.
        modes["transit"] = {"transit": [{"mode": m} for m in transit],
                            "access": ["WALK"], "egress": ["WALK"], "transfer": ["WALK"]}
        modes["direct"] = [m for m in street if m in ("WALK", "BICYCLE", "CAR")]
    else:
        modes["direct"] = [m for m in street if m in ("WALK", "BICYCLE", "CAR")] or ["WALK"]
        modes["directOnly"] = True
    prefs: dict = {"accessibility": {"wheelchair": {"enabled": wheelchair}}}
    if walk_reluctance is not None:
        prefs["street"] = {"walk": {"reluctance": walk_reluctance}}
    return {
        "origin": {"location": {"coordinate": {"latitude": from_lat, "longitude": from_lon}}},
        "destination": {"location": {"coordinate": {"latitude": to_lat, "longitude": to_lon}}},
        "dateTime": {"latestArrival" if arrive_by else "earliestDeparture": when.isoformat()},
        "modes": modes, "first": num, "preferences": prefs, "locale": locale,
    }


@router.get("/v1/cities/{city}/plan", response_model=PlanResponse, response_model_by_alias=True)
async def plan(
    rt: CityRuntime = Depends(city_runtime),
    fromLat: float = Query(..., ge=-90, le=90), fromLon: float = Query(..., ge=-180, le=180),
    toLat: float = Query(..., ge=-90, le=90), toLon: float = Query(..., ge=-180, le=180),
    time: str | None = Query(None, description="ISO-8601; default now in the city's timezone"),
    arriveBy: bool = False,
    modes: str | None = Query(None, description="comma list: TRANSIT,WALK,BUS,RAIL,SUBWAY,TRAM,CABLE_CAR,BICYCLE"),
    wheelchair: bool = False,
    numItineraries: int = Query(5, ge=1, le=10),
    maxWalkDistance: int = Query(1500, ge=100, le=10000),
    locale: str = Query("es", pattern="^(es|en)$"),
):
    city = rt.city
    tz = ZoneInfo(city.timezone)
    if time:
        try:
            when = dt.datetime.fromisoformat(time.replace("Z", "+00:00"))
        except ValueError as e:
            raise ApiError(f"time: {e}") from e
        when = when.replace(tzinfo=tz) if when.tzinfo is None else when.astimezone(tz)
    else:
        when = dt.datetime.now(tz)
    transit, street = parse_modes(modes, city.transit_modes())
    # OTP 2 has no hard walk cap; a longer allowed walk maps to a lower walking reluctance.
    reluctance = max(1.0, min(5.0, 2.0 * 1500 / maxWalkDistance))
    variables = build_variables(from_lat=fromLat, from_lon=fromLon, to_lat=toLat, to_lon=toLon, when=when,
                                arrive_by=arriveBy, transit=transit, street=street, wheelchair=wheelchair,
                                num=numItineraries, locale=locale, walk_reluctance=reluctance)
    data = await rt.otp.graphql(PLAN_QUERY, variables, locale=locale)
    origin = {"name": None, "lat": fromLat, "lon": fromLon}
    dest = {"name": None, "lat": toLat, "lon": toLon}
    return plan_from_otp(city, data, origin, dest, rt.otp.version, locale)
