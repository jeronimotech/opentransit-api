import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request

from ..errors import ApiError
from ..forecast import (
    MAX_FANOUT,
    ForecastCache,
    annotate_gaps,
    build_notes,
    build_options,
    mark_recommended,
    sample_times,
)
from ..gbfs import GbfsNetwork as GbfsNetworkForms
from ..geocode import reverse
from ..models import ForecastResponse, PlanResponse
from ..normalize import apply_endpoint_names, enrich_rental, plan_from_otp
from ..ondemand import attach_to_plan, haversine_m, is_ondemand_leg
from ..otp import PLAN_QUERY
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["planning"])

STREET = {"WALK", "BICYCLE", "CAR", "SCOOTER"}
RENTAL = {"BIKE_RENTAL": "BICYCLE_RENTAL", "BICYCLE_RENTAL": "BICYCLE_RENTAL", "SCOOTER_RENTAL": "SCOOTER_RENTAL"}
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
        elif tok in RENTAL:
            if RENTAL[tok] not in street:
                street.append(RENTAL[tok])
        else:
            raise ApiError(f"unknown mode '{tok}'")
    if not street:
        street = ["WALK"]
    return transit, street


RENTAL_MODES = ("BICYCLE_RENTAL", "SCOOTER_RENTAL")
# The rental-biased companion search (see merge_plans): walking is made expensive and cycling cheap so OTP
# surfaces "rent a bike to the station" access legs that the balanced search drops as slightly slower.
RENTAL_BIAS = {"walk_reluctance": 5.0, "bicycle_reluctance": 1.0}


def build_variables(*, from_lat: float, from_lon: float, to_lat: float, to_lon: float, when: dt.datetime,
                    arrive_by: bool, transit: list[str], street: list[str], wheelchair: bool,
                    num: int, locale: str, walk_reluctance: float | None,
                    bicycle_reluctance: float | None = None, access_extra: list[str] | None = None,
                    egress_extra: list[str] | None = None) -> dict:
    modes: dict = {}
    rental = [m for m in street if m in RENTAL_MODES]
    if len(rental) > 1:
        # OTP 2.9: at most two modes per leg and every rental mode must be paired with WALK, so a query can
        # carry ONE rental mode; the router runs one query per rental mode and merges (merge_plans).
        raise ValueError("at most one rental mode per OTP query")
    direct = [m for m in street if m in ("WALK", "BICYCLE", "CAR")] + rental
    if transit:
        # Access/egress stay on foot: feeds rarely declare bikes_allowed, and OTP then finds nothing.
        # A requested BICYCLE is offered as a direct (bike-only) alternative next to the transit options.
        # Shared vehicles (GBFS) are allowed as access/egress AND as a direct alternative.
        # v1.4 on-demand combos: OTP's kiss-and-ride modes (CAR_DROP_OFF access, CAR_PICKUP egress) must be
        # paired with WALK in the same list.
        modes["transit"] = {"transit": [{"mode": m} for m in transit],
                            "access": ["WALK", *rental, *(access_extra or [])],
                            "egress": ["WALK", *rental, *(egress_extra or [])], "transfer": ["WALK"]}
        modes["direct"] = direct
    else:
        modes["direct"] = direct or ["WALK"]
        modes["directOnly"] = True
    prefs: dict = {"accessibility": {"wheelchair": {"enabled": wheelchair}}}
    if walk_reluctance is not None:
        prefs.setdefault("street", {})["walk"] = {"reluctance": walk_reluctance}
    if bicycle_reluctance is not None:
        prefs.setdefault("street", {})["bicycle"] = {"reluctance": bicycle_reluctance}
    return {
        "origin": {"location": {"coordinate": {"latitude": from_lat, "longitude": from_lon}}},
        "destination": {"location": {"coordinate": {"latitude": to_lat, "longitude": to_lon}}},
        "dateTime": {"latestArrival" if arrive_by else "earliestDeparture": when.isoformat()},
        "modes": modes, "first": num, "preferences": prefs, "locale": locale,
    }


def resolve_rental_modes(street: list[str], availability: dict[str, bool | None]) -> tuple[list[str], list[str]]:
    """Drop requested rental modes that no configured network can serve right now.
    `availability`: mode -> True/False, or None when no network has loaded its status yet (then we trust the
    configuration and keep the mode). Returns (street modes, warnings)."""
    warnings: list[str] = []
    out: list[str] = []
    for m in street:
        if m in RENTAL_MODES and availability.get(m) is False:
            warnings.append(f"MODE_NO_VEHICLES: {m}")
            continue
        out.append(m)
    return (out or ["WALK"]), warnings


def rental_availability(rt: CityRuntime) -> dict[str, bool | None]:
    """mode -> whether any configured network has vehicles of that family available (None = unknown yet)."""
    out: dict[str, bool | None] = {}
    for mode in RENTAL_MODES:
        family = "scooter" if mode == "SCOOTER_RENTAL" else "bicycle"
        verdicts = []
        for g in rt.gbfs.values():
            if family not in [GbfsNetworkForms.form_factor_of(f) for f in g.cfg.form_factors]:
                continue
            verdicts.append(g.mode_available(mode))
        if not verdicts:
            out[mode] = False if rt.gbfs else None
        elif any(v is True for v in verdicts):
            out[mode] = True
        elif all(v is False for v in verdicts):
            out[mode] = False
        else:
            out[mode] = None
    return out


def _signature(it: dict) -> tuple:
    sig = []
    for lg in it.get("legs") or []:
        f, t = lg.get("from") or {}, lg.get("to") or {}
        sig.append((lg.get("mode"), (lg.get("route") or {}).get("id"), f.get("stopId") or f.get("rentalStationId"),
                    t.get("stopId") or t.get("rentalStationId"), (lg.get("startTime") or "")[:16]))
    return tuple(sig)


def merge_plans(primary: list[dict], rental_searches: list[list[dict]], num: int, *, min_rental: int = 2) -> list[dict]:
    """Merge the balanced search with the rental-oriented ones.

    Rules: the primary results come first (up to `num`); rental itineraries (those with at least one rental
    leg) from the other searches are added when not already present; if any rental itinerary exists, at least
    the best `min_rental` (shortest) are guaranteed a place; the total is capped at `num + 2` by dropping the
    lowest-ranked non-rental primary results; the final list is sorted by arrival time and re-numbered."""
    seen: set[tuple] = set()
    chosen: list[dict] = []
    for it in primary[:num]:
        sig = _signature(it)
        if sig in seen:
            continue
        seen.add(sig)
        it["source"] = "primary"
        chosen.append(it)
    candidates: list[dict] = []
    for search in rental_searches:
        for it in search:
            if not it.get("rentalLegs"):
                continue
            sig = _signature(it)
            if sig in seen:
                continue
            seen.add(sig)
            it["source"] = "rental"
            candidates.append(it)
    already = sum(1 for it in chosen if it.get("rentalLegs"))
    candidates.sort(key=lambda it: it.get("durationSeconds") or 0)
    cap = num + 2
    for it in candidates:
        need = already < min_rental
        if not need and len(chosen) >= cap:
            break
        while len(chosen) >= cap:
            # make room for a guaranteed rental option: drop the worst-ranked non-rental primary result
            idx = next((i for i in range(len(chosen) - 1, -1, -1)
                        if chosen[i]["source"] == "primary" and not chosen[i].get("rentalLegs")), None)
            if idx is None:
                break
            chosen.pop(idx)
        if len(chosen) >= cap:
            break
        chosen.append(it)
        already += 1
    chosen.sort(key=lambda it: (it.get("endTime") or "", it.get("durationSeconds") or 0))
    for i, it in enumerate(chosen):
        it["id"] = f"it-{i}"
    return chosen


def merge_ondemand(chosen: list[dict], ondemand_searches: list[list[dict]], num: int, *,
                   max_feeder_m: float, show_when_transit_faster: bool = True, max_direct: int = 1,
                   max_combos: int = 2) -> list[dict]:
    """Add taxi / ride-hailing itineraries next to the transit ones.

    Candidates are itineraries with a CAR leg: the direct ride (one CAR leg) and first/last-mile combos whose
    CAR leg is within `max_feeder_m`. The best transit itinerary is never displaced; at most `max_direct`
    direct rides and `max_combos` combos (shortest first) are added, capped at `num + 3` by dropping the
    worst-ranked plain transit results; the list is sorted by arrival time and re-numbered."""
    seen = {_signature(it) for it in chosen}
    shapes: set[tuple] = set()          # time-less signature: the same combo at another departure is not new
    direct: list[dict] = []
    combos: list[dict] = []
    for search in ondemand_searches:
        for it in sorted((x for s in [search] for x in s), key=lambda it: it.get("durationSeconds") or 0):
            legs = it.get("legs") or []
            car = [lg for lg in legs if is_ondemand_leg(lg)]
            if not car:
                continue
            sig = _signature(it)
            shape = tuple(x[:4] for x in sig)
            if sig in seen or shape in shapes:
                continue
            seen.add(sig)
            shapes.add(shape)
            it["source"] = "ondemand"
            if any(lg.get("transit") for lg in legs):
                if all((lg.get("distanceMeters") or 0) <= max_feeder_m for lg in car):
                    combos.append(it)
            else:
                direct.append(it)
    best_transit = next((it for it in chosen if any(lg.get("transit") for lg in it.get("legs") or [])), None)
    if best_transit and not show_when_transit_faster:
        limit = best_transit.get("durationSeconds") or 0
        direct = [it for it in direct if (it.get("durationSeconds") or 0) <= limit]
        combos = [it for it in combos if (it.get("durationSeconds") or 0) <= limit]
    direct.sort(key=lambda it: it.get("durationSeconds") or 0)
    combos.sort(key=lambda it: it.get("durationSeconds") or 0)
    picks = direct[:max_direct] + combos[:max_combos]
    cap = num + 3
    for it in picks:
        while len(chosen) >= cap:
            idx = next((i for i in range(len(chosen) - 1, 0, -1)
                        if chosen[i].get("source") == "primary" and not chosen[i].get("rentalLegs")), None)
            if idx is None:
                break
            chosen.pop(idx)
        if len(chosen) >= cap:
            break
        chosen.append(it)
    chosen.sort(key=lambda it: (it.get("endTime") or "", it.get("durationSeconds") or 0))
    for i, it in enumerate(chosen):
        it["id"] = f"it-{i}"
    return chosen


ONDEMAND_TOKEN = "ONDEMAND"


@router.get("/v1/cities/{city}/plan", response_model=PlanResponse, response_model_by_alias=True)
async def plan(
    request: Request,
    rt: CityRuntime = Depends(city_runtime),
    fromLat: float = Query(..., ge=-90, le=90), fromLon: float = Query(..., ge=-180, le=180),
    toLat: float = Query(..., ge=-90, le=90), toLon: float = Query(..., ge=-180, le=180),
    time: str | None = Query(None, description="ISO-8601; default now in the city's timezone"),
    arriveBy: bool = False,
    modes: str | None = Query(None, description="comma list: TRANSIT,WALK,BUS,RAIL,SUBWAY,TRAM,CABLE_CAR,BICYCLE,"
                                                "BIKE_RENTAL,SCOOTER_RENTAL,ONDEMAND"),
    onDemand: bool = Query(False, description="add taxi / ride-hailing options (direct + first/last mile)"),
    wheelchair: bool = False,
    numItineraries: int = Query(5, ge=1, le=10),
    maxWalkDistance: int = Query(1500, ge=100, le=10000),
    locale: str = Query("es", pattern="^(es|en)$"),
    fromName: str | None = Query(None, max_length=120, description="label for the origin, echoed back"),
    toName: str | None = Query(None, max_length=120, description="label for the destination, echoed back"),
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
    if modes and ONDEMAND_TOKEN in {t.strip().upper() for t in modes.split(",")}:
        onDemand = True
        modes = ",".join(t for t in modes.split(",") if t.strip().upper() != ONDEMAND_TOKEN) or None
    on_demand = onDemand and city.on_demand_enabled()
    transit, street = parse_modes(modes, city.transit_modes())
    if any(m in RENTAL_MODES for m in street) and not city.mobility.bike_share:
        raise ApiError(f"shared vehicles are not available in {city.name}", code="MODE_UNAVAILABLE")
    street, mode_warnings = resolve_rental_modes(street, rental_availability(rt))
    rental = [m for m in street if m in RENTAL_MODES]
    base_street = [m for m in street if m not in RENTAL_MODES] or ["WALK"]
    # OTP 2 has no hard walk cap; a longer allowed walk maps to a lower walking reluctance.
    reluctance = max(1.0, min(5.0, 2.0 * 1500 / maxWalkDistance))
    common = dict(from_lat=fromLat, from_lon=fromLon, to_lat=toLat, to_lon=toLon, when=when, arrive_by=arriveBy,
                  transit=transit, wheelchair=wheelchair, locale=locale)
    # One OTP query per rental mode (OTP allows a single rental mode per leg, always paired with WALK). The first
    # rental mode rides along the balanced primary search; with transit, every rental mode also gets a
    # rental-biased companion search so "bike to the station" options are not lost to walking (merge_plans).
    searches: list[dict] = [build_variables(**common, street=base_street + rental[:1], num=numItineraries,
                                            walk_reluctance=reluctance)]
    for m in rental[1:]:
        searches.append(build_variables(**common, street=["WALK", m], num=numItineraries,
                                        walk_reluctance=reluctance))
    if transit:
        for m in rental:
            searches.append(build_variables(**common, street=["WALK", m], num=max(6, numItineraries + 4),
                                            walk_reluctance=RENTAL_BIAS["walk_reluctance"],
                                            bicycle_reluctance=RENTAL_BIAS["bicycle_reluctance"]))
    # v1.4 on-demand: a direct car ride (within the policy distance) and, with transit, kiss-and-ride combos
    # (car to the first stop / car from the last stop, bounded by maxFeederKm after the fact).
    od_start = len(searches)
    policy = city.mobility.on_demand_policy
    if on_demand:
        if haversine_m(fromLat, fromLon, toLat, toLon) <= policy.max_direct_distance_km * 1000:
            searches.append(build_variables(**{**common, "transit": []}, street=["CAR"], num=1,
                                            walk_reluctance=None))
        if policy.first_last_mile and transit:
            n = max(4, numItineraries)
            searches.append(build_variables(**common, street=base_street, num=n, walk_reluctance=reluctance,
                                            access_extra=["CAR_DROP_OFF"]))
            searches.append(build_variables(**common, street=base_street, num=n, walk_reluctance=reluctance,
                                            egress_extra=["CAR_PICKUP"]))
    # Names: the caller's label wins; otherwise a reverse geocode runs concurrently with the plan and is
    # only used if it comes back within a short budget, so it never adds latency to the itinerary search.
    results = await asyncio.gather(
        *(rt.otp.graphql(PLAN_QUERY, v, locale=locale) for v in searches),
        _cheap_reverse(city, fromLat, fromLon, skip=bool(fromName)),
        _cheap_reverse(city, toLat, toLon, skip=bool(toName)))
    datas, rev_from, rev_to = list(results[:-2]), results[-2], results[-1]
    origin = {"name": fromName or rev_from, "lat": fromLat, "lon": fromLon}
    dest = {"name": toName or rev_to, "lat": toLat, "lon": toLon}
    plans = [plan_from_otp(city, d, origin, dest, rt.otp.version, locale, rt.rental_prices()) for d in datas]
    plan_out = plans[0]
    base_plans, od_plans = plans[:od_start], plans[od_start:]
    if len(base_plans) > 1:
        plan_out["itineraries"] = merge_plans(base_plans[0]["itineraries"],
                                             [p["itineraries"] for p in base_plans[1:]], numItineraries)
    if od_plans:
        for it in plan_out["itineraries"]:
            it.setdefault("source", "primary")
        plan_out["itineraries"] = merge_ondemand(plan_out["itineraries"], [p["itineraries"] for p in od_plans],
                                                 numItineraries, max_feeder_m=policy.max_feeder_km * 1000,
                                                 show_when_transit_faster=policy.show_when_transit_faster)
    if len(plans) > 1:
        plan_out["warnings"] = [w for w in plan_out["warnings"]
                                if not (w.startswith("NO_ITINERARIES") and plan_out["itineraries"])]
    plan_out["warnings"] = mode_warnings + plan_out["warnings"]
    for it in plan_out["itineraries"]:
        for leg in it["legs"]:
            rt.with_window(leg.get("route"))
    enrich_rental(plan_out, rt.rental_lookup)
    if on_demand:
        attach_to_plan(city, plan_out, when=when, base_url=str(request.base_url), locale=locale)
    return apply_endpoint_names(plan_out, origin["name"], dest["name"])


async def _cheap_reverse(city, lat: float, lon: float, *, skip: bool, budget_s: float = 1.5) -> str | None:
    if skip:
        return None
    try:
        r = await asyncio.wait_for(reverse(city, lat, lon), timeout=budget_s)
        return r.get("name")
    except (TimeoutError, Exception):  # noqa: BLE001
        return None


@router.get("/v1/cities/{city}/plan/forecast", response_model=ForecastResponse,
            response_model_by_alias=True)
async def plan_forecast(
    request: Request,
    rt: CityRuntime = Depends(city_runtime),
    fromLat: float = Query(..., ge=-90, le=90), fromLon: float = Query(..., ge=-180, le=180),
    toLat: float = Query(..., ge=-90, le=90), toLon: float = Query(..., ge=-180, le=180),
    time: str | None = Query(None, description="ISO-8601 start of the window; default now"),
    modes: str | None = Query(None),
    windowMinutes: int = Query(90, ge=15, le=360),
    maxOptions: int = Query(8, ge=2, le=12),
    arriveBy: bool = False,
    wheelchair: bool = False,
    locale: str = Query("es", pattern="^(es|en)$"),
    fromName: str | None = Query(None, max_length=120),
    toName: str | None = Query(None, max_length=120),
):
    """"¿Cuándo salir?": one row per distinct departure across the window, with gaps and service notes.

    Bounded fan-out (`forecast.MAX_FANOUT` upstream plans) and a 60 s cache on the rounded query keep this
    from multiplying router load when a user scrubs the sheet."""
    city = rt.city
    tz = ZoneInfo(city.timezone)
    if time:
        try:
            start = dt.datetime.fromisoformat(time.replace("Z", "+00:00"))
        except ValueError as e:
            raise ApiError(f"time: {e}") from e
        start = start.replace(tzinfo=tz) if start.tzinfo is None else start.astimezone(tz)
    else:
        start = dt.datetime.now(tz)
    cache: ForecastCache = request.app.state.forecast_cache
    ckey = ForecastCache.key(city.id, from_lat=fromLat, from_lon=fromLon, to_lat=toLat, to_lon=toLon,
                             when=start, window=windowMinutes, modes=modes, arrive_by=arriveBy,
                             max_options=maxOptions, locale=locale)
    origin = {"name": fromName, "lat": fromLat, "lon": fromLon}
    dest = {"name": toName, "lat": toLat, "lon": toLon}
    cached = cache.get(ckey)
    if cached is not None:
        # The cache holds the computed window only: labels are per-caller, so one user's "Casa" must never
        # be served to the next person asking for the same corridor.
        return {**cached, "from": origin, "to": dest}

    transit, street = parse_modes(modes, city.transit_modes())
    street = [m for m in street if m not in RENTAL_MODES] or ["WALK"]
    when_list = sample_times(start, windowMinutes, MAX_FANOUT)
    common = dict(from_lat=fromLat, from_lon=fromLon, to_lat=toLat, to_lon=toLon, arrive_by=arriveBy,
                  transit=transit, street=street, wheelchair=wheelchair, locale=locale,
                  num=2, walk_reluctance=2.0)
    datas = await asyncio.gather(
        *(rt.otp.graphql(PLAN_QUERY, build_variables(**common, when=w), locale=locale) for w in when_list),
        return_exceptions=True)
    plans: list[list[dict]] = []
    for d in datas:
        if isinstance(d, Exception):
            continue                      # one slow probe must not sink the whole window
        plans.append(plan_from_otp(city, d, origin, dest, rt.otp.version, locale,
                                   rt.rental_prices())["itineraries"])
    options = build_options(plans, max_options=maxOptions, arrive_by=arriveBy)
    annotate_gaps(options)
    mark_recommended(options)
    first_route = next((r for o in options for r in o["routeIds"]), None)
    notes = build_notes(options, window_end=start + dt.timedelta(minutes=windowMinutes),
                        service_window=rt.service_window(first_route), locale=locale)
    body = ForecastResponse(**{
        "from": origin, "to": dest,
        "generatedAt": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "windowMinutes": windowMinutes, "options": options, "notes": notes,
    }).model_dump(by_alias=True)
    cache.put(ckey, {k: v for k, v in body.items() if k not in ("from", "to")})
    return body
