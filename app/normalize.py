"""Translate OTP GraphQL payloads into the public contract. Nothing raw from OTP leaves this module."""
import re

from .cities import City
from .features import accessibility_block, estimate_fare, infer_severity

# Per-city flags learned at ingest (e.g. wheelchairUnverified). Set from main/admin after each ingest.
_FEED_FLAGS: dict[str, dict] = {}


def set_feed_flags(city_id: str, flags: dict | None) -> None:
    _FEED_FLAGS[city_id] = dict(flags or {})


def feed_flags(city_id: str) -> dict:
    return _FEED_FLAGS.get(city_id, {})

_DUR = re.compile(r"^(-)?P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")
_WHEELCHAIR = {"POSSIBLE": "accessible", "NOT_POSSIBLE": "not_accessible", "NO_INFORMATION": "unknown",
               1: "accessible", 2: "not_accessible", 0: "unknown", None: "unknown"}
_LOCATION = {"STATION": "station", "ENTRANCE": "entrance", "STOP": "stop", 1: "station", 2: "entrance", 0: "stop"}
_STEP_WORDS_ES = {
    "DEPART": "Sal por", "CONTINUE": "Continúa por", "LEFT": "Gira a la izquierda en",
    "RIGHT": "Gira a la derecha en", "SLIGHTLY_LEFT": "Gira levemente a la izquierda en",
    "SLIGHTLY_RIGHT": "Gira levemente a la derecha en", "HARD_LEFT": "Gira fuerte a la izquierda en",
    "HARD_RIGHT": "Gira fuerte a la derecha en", "UTURN_LEFT": "Da la vuelta en", "UTURN_RIGHT": "Da la vuelta en",
    "ENTER_STATION": "Entra a la estación", "EXIT_STATION": "Sal de la estación", "ELEVATOR": "Toma el ascensor",
    "FOLLOW_SIGNS": "Sigue las señales hacia", "CIRCLE_CLOCKWISE": "Toma la glorieta hacia",
    "CIRCLE_COUNTERCLOCKWISE": "Toma la glorieta hacia",
}
_STEP_WORDS_EN = {
    "DEPART": "Head along", "CONTINUE": "Continue on", "LEFT": "Turn left onto", "RIGHT": "Turn right onto",
    "SLIGHTLY_LEFT": "Bear left onto", "SLIGHTLY_RIGHT": "Bear right onto", "HARD_LEFT": "Turn sharp left onto",
    "HARD_RIGHT": "Turn sharp right onto", "UTURN_LEFT": "Make a U-turn onto", "UTURN_RIGHT": "Make a U-turn onto",
    "ENTER_STATION": "Enter the station", "EXIT_STATION": "Exit the station", "ELEVATOR": "Take the elevator",
    "FOLLOW_SIGNS": "Follow signs to", "CIRCLE_CLOCKWISE": "Take the roundabout to",
    "CIRCLE_COUNTERCLOCKWISE": "Take the roundabout to",
}


def parse_duration(s: str | None) -> int | None:
    """ISO-8601 duration (java.time.Duration) -> seconds."""
    if not s:
        return None
    m = _DUR.match(s)
    if not m:
        return None
    neg, d, h, mi, sec = m.groups()
    total = int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)
    return int(-total if neg else total)


def route_ref(city: City, r: dict | None, gtfs_id: str | None = None) -> dict | None:
    if not r:
        return None
    agency = (r.get("agency") or {}).get("gtfsId")
    agency_raw = city.unscoped(agency) if agency else None
    color = r.get("color")
    text = r.get("textColor")
    return {
        "id": r.get("gtfsId") or gtfs_id, "shortName": r.get("shortName"), "longName": r.get("longName"),
        "color": f"#{color}" if color and not color.startswith("#") else color,
        "textColor": f"#{text}" if text and not text.startswith("#") else text,
        "mode": r.get("mode") or "BUS", "agencyId": agency_raw,
        "component": city.component_of_agency(agency_raw),
    }


def route_ref_from_db(city: City, row: dict | None, raw_id: str | None = None) -> dict | None:
    if not row:
        return None
    return {
        "id": city.scoped(row.get("route_id") or raw_id), "shortName": row.get("short_name"),
        "longName": row.get("long_name"), "color": row.get("color"), "textColor": row.get("text_color"),
        "mode": gtfs_route_type_to_mode(row.get("route_type")), "agencyId": row.get("agency_id"),
        "component": row.get("component") or city.component_of_agency(row.get("agency_id")),
    }


def gtfs_route_type_to_mode(t: int | None) -> str:
    return {0: "TRAM", 1: "SUBWAY", 2: "RAIL", 3: "BUS", 4: "FERRY", 5: "CABLE_CAR", 6: "GONDOLA",
            7: "FUNICULAR", 11: "TROLLEYBUS", 12: "MONORAIL"}.get(t or 3, "BUS")


def stop_from_otp(city: City, s: dict | None) -> dict | None:
    if not s:
        return None
    parent = (s.get("parentStation") or {}).get("gtfsId")
    return {
        "id": s["gtfsId"], "code": s.get("code"), "name": s.get("name") or s["gtfsId"],
        "lat": s.get("lat"), "lon": s.get("lon"),
        "locationType": _LOCATION.get(s.get("locationType"), "stop"),
        "component": None, "wheelchair": _WHEELCHAIR.get(s.get("wheelchairBoarding"), "unknown"),
        "accessibility": accessibility_block(_WHEELCHAIR.get(s.get("wheelchairBoarding"), "unknown"),
                                             bool(feed_flags(city.id).get("wheelchairUnverified"))),
        "parentStationId": parent,
    }


def clean_headsign(headsign: str | None, route_short_name: str | None) -> str | None:
    """Bogotá's feed sets trip_headsign to the route's own short name ("G12"), which is
    useless as a direction. Return None in that case so clients fall back to something
    meaningful (route long name, last stop of the pattern)."""
    if not headsign:
        return None
    h = headsign.strip()
    if route_short_name and h.casefold() == route_short_name.strip().casefold():
        return None
    return h or None


def pattern_from_otp(city: City, p: dict, route_short_name: str | None = None) -> dict:
    """Pattern with a usable headsign (falls back to the last stop) and directionId in {0, 1, None}."""
    stops = [stop_from_otp(city, s) for s in (p.get("stops") or []) if s]
    geom = (p.get("patternGeometry") or {}).get("points")
    d = p.get("directionId")
    return {
        "id": p["code"],
        "headsign": clean_headsign(p.get("headsign"), route_short_name) or (stops[-1]["name"] if stops else None),
        "directionId": d if d in (0, 1) else None,
        "geometry": {"encoded": geom, "precision": 5} if geom else None,
        "stops": stops,
    }


def merge_departures(deps: list[dict]) -> list[dict]:
    """Dedupe by tripId (a station's child stops share trips) and sort by effective time."""
    seen: set[str] = set()
    out = []
    for d in sorted(deps, key=lambda d: d.get("realtimeTime") or d["scheduledTime"]):
        key = d.get("tripId")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(d)
    return out


def apply_endpoint_names(plan: dict, from_name: str | None, to_name: str | None) -> dict:
    """Replace OTP's generic 'Origin'/'Destination' labels with the caller's or reverse-geocoded names."""
    for key, name, leg_idx, place_key in (("from", from_name, 0, "from"), ("to", to_name, -1, "to")):
        if not name:
            continue
        plan[key]["name"] = name
        for it in plan.get("itineraries", []):
            legs = it.get("legs") or []
            if legs and not legs[leg_idx][place_key].get("stopId"):
                legs[leg_idx][place_key]["name"] = name
    return plan


def stop_from_db(city: City, row: dict) -> dict:
    return {
        "id": city.scoped(row["stop_id"]), "code": row.get("stop_code"), "name": (row["name"] or "").strip(),
        "lat": row["lat"], "lon": row["lon"], "locationType": _LOCATION.get(row.get("location_type"), "stop"),
        "component": row.get("component"), "wheelchair": _WHEELCHAIR.get(row.get("wheelchair"), "unknown"),
        "accessibility": accessibility_block(_WHEELCHAIR.get(row.get("wheelchair"), "unknown"),
                                             bool(feed_flags(city.id).get("wheelchairUnverified"))),
        "parentStationId": city.scoped(row["parent_station"]) if row.get("parent_station") else None,
    }


def alert_from_otp(city: City, a: dict | None) -> dict | None:
    if not a:
        return None
    from .rt import iso
    routes = [e["gtfsId"] for e in (a.get("entities") or []) if e and e.get("__typename") == "Route"]
    stops = [e["gtfsId"] for e in (a.get("entities") or []) if e and e.get("__typename") == "Stop"]
    return {
        "id": str(a.get("id")), "cause": a.get("alertCause"), "effect": a.get("alertEffect"),
        "severity": infer_severity(a.get("alertSeverityLevel"), a.get("alertEffect")),
        "severitySource": "feed" if a.get("alertSeverityLevel") in ("INFO", "WARNING", "SEVERE") else "inferred",
        "header": a.get("alertHeaderText"),
        "description": a.get("alertDescriptionText") or None, "url": a.get("alertUrl"),
        "start": iso(a.get("effectiveStartDate")), "end": iso(a.get("effectiveEndDate")),
        "routeIds": routes, "stopIds": stops, "routes": [],
    }


def _time(lt: dict | None) -> tuple[str | None, str | None, int | None]:
    """LegTime -> (effective time, scheduled time, delay seconds)."""
    if not lt:
        return None, None, None
    est = lt.get("estimated")
    if est and est.get("time"):
        return est["time"], lt.get("scheduledTime"), parse_duration(est.get("delay"))
    return lt.get("scheduledTime"), lt.get("scheduledTime"), None


def _count(v) -> int | None:
    """OTP 2.9 counts are objects (`{total, byType}`); older versions returned plain ints."""
    if isinstance(v, dict):
        return v.get("total")
    return v


def rental_station_ref(city: City, st: dict | None) -> dict | None:
    """OTP VehicleRentalStation -> contract RentalStationRef (ids scoped with OUR network id)."""
    if not st or not st.get("stationId"):
        return None
    net = city.bike_network((st.get("rentalNetwork") or {}).get("networkId"))
    raw = st["stationId"]
    raw = raw.split(":", 1)[1] if net and raw.startswith(net.network + ":") else raw
    return {"stationId": f"{net.id}:{raw}" if net else raw, "name": st.get("name"),
            "lat": st.get("lat"), "lon": st.get("lon"),
            "vehiclesAvailable": _count(st.get("availableVehicles")),
            "docksAvailable": _count(st.get("availableSpaces")),
            "lastReported": None, "_otpNetwork": (st.get("rentalNetwork") or {}).get("networkId"), "_raw": raw}


def place_from_otp(city: City, p: dict | None, component: str | None = None) -> dict | None:
    if not p:
        return None
    stop = p.get("stop") or {}
    arr, _, _ = _time(p.get("arrival"))
    dep, _, _ = _time(p.get("departure"))
    rental = rental_station_ref(city, p.get("vehicleRentalStation"))
    return {"name": p.get("name"), "lat": p.get("lat"), "lon": p.get("lon"),
            "stopId": stop.get("gtfsId"), "stopCode": stop.get("code"),
            "arrival": arr, "departure": dep, "component": component if stop else None,
            "rentalStationId": rental["stationId"] if rental else None}


_VEHICLE_TYPE = {("BICYCLE", "ELECTRIC_ASSIST"): "electric_assist", ("BICYCLE", "ELECTRIC"): "electric_assist",
                 ("SCOOTER", None): "scooter", ("SCOOTER_STANDING", None): "scooter",
                 ("SCOOTER_SEATED", None): "scooter"}


def rental_from_otp(city: City, leg: dict, rental_prices: dict | None = None) -> dict | None:
    """Rental block for a leg on a shared vehicle: OTP marks these with `rentedBike` and/or rental places."""
    from_p, to_p = leg.get("from") or {}, leg.get("to") or {}
    pickup_st, drop_st = from_p.get("vehicleRentalStation"), to_p.get("vehicleRentalStation")
    vehicle = from_p.get("rentalVehicle") or to_p.get("rentalVehicle")
    if not (leg.get("rentedBike") or pickup_st or drop_st or vehicle):
        return None
    if leg.get("mode") not in ("BICYCLE", "SCOOTER", None):
        return None
    otp_net = None
    for src in (pickup_st, drop_st, vehicle):
        if src and (src.get("rentalNetwork") or {}).get("networkId"):
            otp_net = src["rentalNetwork"]["networkId"]
            break
    net = city.bike_network(otp_net)
    vt = (vehicle or {}).get("vehicleType") or {}
    form, prop = (vt.get("formFactor") or "").upper() or None, (vt.get("propulsionType") or "").upper() or None
    vtype = _VEHICLE_TYPE.get((form, prop)) or _VEHICLE_TYPE.get((form, None)) \
        or ("bicycle" if form == "BICYCLE" or leg.get("mode") == "BICYCLE" else None)
    price = (rental_prices or {}).get(net.id) if net else None
    return {
        "networkId": net.id if net else (otp_net or "unknown"),
        "networkName": net.name if net else otp_net, "color": net.color if net else None,
        "vehicleType": vtype,
        "pickup": rental_station_ref(city, pickup_st), "dropoff": rental_station_ref(city, drop_st),
        "freeFloating": bool(vehicle) and not pickup_st,
        "priceEstimate": dict(price) if price else None,
    }


def _mode_used(leg: dict) -> str:
    if leg.get("rental"):
        return "SCOOTER_RENTAL" if leg["rental"].get("vehicleType") == "scooter" else "BICYCLE_RENTAL"
    return leg.get("mode") or "WALK"


def _instruction(step: dict, locale: str) -> str:
    words = _STEP_WORDS_EN if locale.startswith("en") else _STEP_WORDS_ES
    verb = words.get(step.get("relativeDirection") or "CONTINUE", words["CONTINUE"])
    street = step.get("streetName") or ("camino" if not locale.startswith("en") else "path")
    return f"{verb} {street}"


def leg_from_otp(city: City, leg: dict, locale: str = "es", rental_prices: dict | None = None) -> dict:
    start, _, start_delay = _time(leg.get("start"))
    end, _, end_delay = _time(leg.get("end"))
    route = route_ref(city, leg.get("route"))
    component = route["component"] if route else None
    agency = leg.get("agency") or (leg.get("route") or {}).get("agency")
    geom = leg.get("legGeometry") or {}
    realtime = bool(leg.get("realTime")) or (leg.get("start") or {}).get("estimated") is not None
    return {
        "mode": leg.get("mode") or ("WALK" if not leg.get("transitLeg") else "BUS"),
        "transit": bool(leg.get("transitLeg")),
        "startTime": start, "endTime": end,
        "durationSeconds": int(leg.get("duration") or 0), "distanceMeters": round(leg.get("distance") or 0, 1),
        "from": place_from_otp(city, leg.get("from"), component),
        "to": place_from_otp(city, leg.get("to"), component),
        "route": route, "headsign": clean_headsign(leg.get("headsign"), (route or {}).get("shortName")),
        "agency": {"id": city.unscoped(agency["gtfsId"]) if agency.get("gtfsId") else None,
                   "name": agency.get("name")} if agency else None,
        "tripId": (leg.get("trip") or {}).get("gtfsId"),
        "realtime": realtime,
        "realtimeState": leg.get("realtimeState") if leg.get("transitLeg") else None,
        "delaySeconds": start_delay if start_delay is not None else end_delay,
        "geometry": {"encoded": geom["points"], "precision": 5} if geom.get("points") else None,
        "intermediateStops": [
            {"name": s.get("name"), "lat": s.get("lat"), "lon": s.get("lon"), "stopId": s.get("gtfsId"),
             "stopCode": s.get("code"), "arrival": None, "departure": None, "component": component}
            for s in (leg.get("intermediateStops") or []) if s],
        "steps": [
            {"instruction": _instruction(s, locale), "distanceMeters": round(s.get("distance") or 0, 1),
             "lat": s.get("lat"), "lon": s.get("lon"), "relativeDirection": s.get("relativeDirection"),
             "absoluteDirection": s.get("absoluteDirection"), "streetName": s.get("streetName")}
            for s in (leg.get("steps") or []) if s],
        "alerts": [alert_from_otp(city, a) for a in (leg.get("alerts") or []) if a],
        "rental": rental_from_otp(city, leg, rental_prices),
    }


def itinerary_from_otp(city: City, it: dict, idx: int, locale: str = "es",
                       rental_prices: dict | None = None) -> dict:
    legs = [leg_from_otp(city, leg, locale, rental_prices) for leg in (it.get("legs") or []) if leg]
    score = it.get("accessibilityScore")
    modes_used: list[str] = []
    for lg in legs:
        m = _mode_used(lg)
        if m not in modes_used:
            modes_used.append(m)
    return {
        "id": f"it-{idx}", "startTime": it.get("start"), "endTime": it.get("end"),
        "durationSeconds": int(it.get("duration") or 0),
        "walkDistanceMeters": round(it.get("walkDistance") or 0, 1),
        "walkTimeSeconds": int(it.get("walkTime") or 0), "waitingTimeSeconds": int(it.get("waitingTime") or 0),
        "transfers": int(it.get("numberOfTransfers") or 0),
        # No supported city publishes GTFS fares: this is a flat-fare *estimate* from city.fares (or null).
        "fare": estimate_fare(city, legs, locale),
        "accessible": None if score is None else score >= 0.99,
        "rentalLegs": sum(1 for lg in legs if lg.get("rental")),
        "modesUsed": modes_used,
        "source": "primary",        # diagnostic: which search produced it (see routers/plan.py merge_plans)
        "legs": legs,
    }


def plan_from_otp(city: City, data: dict, origin: dict, destination: dict, version: str | None,
                  locale: str = "es", rental_prices: dict | None = None) -> dict:
    conn = data.get("planConnection") or {}
    its = [itinerary_from_otp(city, e["node"], i, locale, rental_prices)
           for i, e in enumerate(conn.get("edges") or []) if e]
    warnings = [f"{err.get('code')}: {err.get('description')}" for err in (conn.get("routingErrors") or [])]
    if not its and not warnings:
        warnings.append("NO_ITINERARIES: no itineraries found for this search")
    return {"from": origin, "to": destination, "itineraries": its,
            "router": {"engine": "otp", "version": version, "realtime": True}, "warnings": warnings}


def departure_from_otp(city: City, st: dict) -> dict:
    day = int(st.get("serviceDay") or 0)
    sched = day + int(st.get("scheduledDeparture") or 0)
    rt = bool(st.get("realtime"))
    rt_dep = day + int(st.get("realtimeDeparture") or 0) if rt else None
    trip = st.get("trip") or {}
    from .rt import iso  # local import to avoid a cycle at import time
    return {
        "route": route_ref(city, trip.get("route")) or {"id": "", "mode": "BUS"},
        "headsign": clean_headsign(st.get("headsign") or trip.get("tripHeadsign"),
                                   (trip.get("route") or {}).get("shortName")),
        "tripId": trip.get("gtfsId"),
        "scheduledTime": iso(sched), "realtimeTime": iso(rt_dep) if rt_dep else None,
        "realtime": rt, "delaySeconds": st.get("departureDelay") if rt else None,
        "canceled": st.get("realtimeState") == "CANCELED", "vehicleId": None,
        "stopSequence": st.get("stopPositionInPattern"),
    }


def enrich_rental(plan: dict, lookup) -> dict:
    """Fill pickup/drop-off availability from the API's own GBFS cache (fresher than OTP's copy) and strip
    the private helper keys. `lookup(otp_network, raw_station_id) -> public station dict | None`."""
    for it in plan.get("itineraries", []):
        for leg in it.get("legs", []):
            r = leg.get("rental")
            if not r:
                continue
            for key in ("pickup", "dropoff"):
                ref = r.get(key)
                if not ref:
                    continue
                live = lookup(ref.pop("_otpNetwork", None), ref.pop("_raw", None))
                if live:
                    ref["name"] = live.get("name") or ref.get("name")
                    ref["vehiclesAvailable"] = live.get("vehiclesAvailable")
                    ref["docksAvailable"] = live.get("docksAvailable")
                    ref["lastReported"] = live.get("lastReported")
    return plan
