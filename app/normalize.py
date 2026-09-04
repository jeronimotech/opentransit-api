"""Translate OTP GraphQL payloads into the public contract. Nothing raw from OTP leaves this module."""
import re

from .cities import City

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
        "parentStationId": parent,
    }


def pattern_from_otp(city: City, p: dict) -> dict:
    """Pattern with a usable headsign (falls back to the last stop) and directionId in {0, 1, None}."""
    stops = [stop_from_otp(city, s) for s in (p.get("stops") or []) if s]
    geom = (p.get("patternGeometry") or {}).get("points")
    d = p.get("directionId")
    return {
        "id": p["code"],
        "headsign": p.get("headsign") or (stops[-1]["name"] if stops else None),
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
        "severity": a.get("alertSeverityLevel"), "header": a.get("alertHeaderText"),
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


def place_from_otp(city: City, p: dict | None, component: str | None = None) -> dict | None:
    if not p:
        return None
    stop = p.get("stop") or {}
    arr, _, _ = _time(p.get("arrival"))
    dep, _, _ = _time(p.get("departure"))
    return {"name": p.get("name"), "lat": p.get("lat"), "lon": p.get("lon"),
            "stopId": stop.get("gtfsId"), "stopCode": stop.get("code"),
            "arrival": arr, "departure": dep, "component": component if stop else None}


def _instruction(step: dict, locale: str) -> str:
    words = _STEP_WORDS_EN if locale.startswith("en") else _STEP_WORDS_ES
    verb = words.get(step.get("relativeDirection") or "CONTINUE", words["CONTINUE"])
    street = step.get("streetName") or ("camino" if not locale.startswith("en") else "path")
    return f"{verb} {street}"


def leg_from_otp(city: City, leg: dict, locale: str = "es") -> dict:
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
        "route": route, "headsign": leg.get("headsign"),
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
    }


def itinerary_from_otp(city: City, it: dict, idx: int, locale: str = "es") -> dict:
    legs = [leg_from_otp(city, leg, locale) for leg in (it.get("legs") or []) if leg]
    score = it.get("accessibilityScore")
    return {
        "id": f"it-{idx}", "startTime": it.get("start"), "endTime": it.get("end"),
        "durationSeconds": int(it.get("duration") or 0),
        "walkDistanceMeters": round(it.get("walkDistance") or 0, 1),
        "walkTimeSeconds": int(it.get("walkTime") or 0), "waitingTimeSeconds": int(it.get("waitingTime") or 0),
        "transfers": int(it.get("numberOfTransfers") or 0),
        "fare": None,  # GTFS-Fares v2 is not published by any supported city yet
        "accessible": None if score is None else score >= 0.99,
        "legs": legs,
    }


def plan_from_otp(city: City, data: dict, origin: dict, destination: dict, version: str | None,
                  locale: str = "es") -> dict:
    conn = data.get("planConnection") or {}
    its = [itinerary_from_otp(city, e["node"], i, locale) for i, e in enumerate(conn.get("edges") or []) if e]
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
        "headsign": st.get("headsign") or trip.get("tripHeadsign"),
        "tripId": trip.get("gtfsId"),
        "scheduledTime": iso(sched), "realtimeTime": iso(rt_dep) if rt_dep else None,
        "realtime": rt, "delaySeconds": st.get("departureDelay") if rt else None,
        "canceled": st.get("realtimeState") == "CANCELED", "vehicleId": None,
        "stopSequence": st.get("stopPositionInPattern"),
    }
