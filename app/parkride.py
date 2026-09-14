"""
Park & Ride (v1.6 phase B): drive your own car to a paid on-street parking zone next to a station, walk,
ride transit.

OTP already plans "car to a stop, then transit" (the CAR_DROP_OFF access mode the taxi combos use). What it
cannot know is where you may *leave* the car: that is the CDS curb inventory — for Bogotá the ZPP zones
PIM publishes with hours, rates and live occupancy. So a park & ride itinerary is a CAR_DROP_OFF itinerary
whose drop-off point has a legal, available parking zone within walking distance, rewritten so the car ends
at that zone, a walk leads from the zone to the stop, and the parking fee for the planned dwell joins the fare.

Nothing here talks to OTP or to the network: the router hands in itineraries and the curb inventory.
"""
from __future__ import annotations

import datetime as dt
import math

from .cities import City
from .geo import encode_polyline
from .openmobility import (
    city_now,
    curb_public,
    distance_to_geometry_m,
    evaluate_zone,
    rule_matches_user_class,
)

WALK_SPEED_MPS = 1.2          # a person carrying things from the car, not a hiker
WALK_DETOUR = 1.25            # straight line -> street distance, the usual urban factor


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parking_fee(rules: list[dict], hours: float) -> float | None:
    """What `hours` of parking cost under a rule's rates, in the inventory's own unit.

    CDS rates may be tiered by `interval_start`/`interval_end` (minutes since arrival): the first two hours at
    one price, the rest at another. A flat rate is one tier without bounds. `maximum_fee` caps a tier.
    Returns None when no rule prices parking at all."""
    minutes = max(0.0, hours * 60)
    for r in rules:
        rates = [x for x in (r.get("rate") or []) if x.get("rate") is not None]
        if not rates or r.get("activity") != "parking":
            continue
        total = 0.0
        for tier in sorted(rates, key=lambda x: x.get("interval_start") or 0):
            lo = float(tier.get("interval_start") or 0)
            hi = tier.get("interval_end")
            hi = float(hi) if hi is not None else float("inf")
            span = max(0.0, min(minutes, hi) - lo)
            if span <= 0:
                continue
            unit = tier.get("rate_unit") or "hour"
            per_minute = {"minute": 1.0, "hour": 1 / 60, "day": 1 / 1440, "week": 1 / 10080}.get(unit)
            if per_minute is None:
                # a per-session or unknown unit: the rate once, not scaled
                cost = float(tier["rate"])
            else:
                cost = float(tier["rate"]) * per_minute * span
            fee = tier.get("maximum_fee")
            if fee is not None:
                cost = min(cost, float(fee))
            total += cost
        return round(total)
    return None


def _winning_rules(zone: dict, policies: dict[str, dict], when: dt.datetime, city: City) -> list[dict]:
    ev = evaluate_zone(zone, policies, when, city, user_class="car")
    if not ev.get("activePolicyIds"):
        return []
    pol = policies.get(ev["activePolicyIds"][0]) or {}
    return [r for r in (pol.get("rules") or []) if rule_matches_user_class(r, "car")]


def find_parking(zones: list[dict], policies: dict[str, dict], lat: float, lon: float, *, when: dt.datetime,
                 city: City, max_walk_m: float, locale: str | None = None) -> dict | None:
    """The nearest zone a car may park in right now, with spaces, within `max_walk_m` of a point — or None.

    A zone that reports zero free spaces is skipped (the whole point is not to arrive and find it full); one
    that reports nothing is allowed, marked so the client can say the count is unknown."""
    best: tuple[float, dict, dict] | None = None
    for z in zones:
        d = distance_to_geometry_m(z.get("geometry"), lon, lat)
        if d is None or d > max_walk_m or (best and d >= best[0]):
            continue
        avail = z.get("available_spaces")
        if avail is not None and int(avail) <= 0:
            continue
        view = curb_public(z, policies, when, city, user_class="car", lat=lat, lon=lon)
        if view.get("allowed") is not True:
            continue
        best = (d, z, view)
    if not best:
        return None
    d, z, view = best
    return {"zone": z, "view": view, "distanceMeters": round(d)}


def _shift(s: str | None, seconds: int) -> str | None:
    if not s:
        return s
    try:
        return (dt.datetime.fromisoformat(s.replace("Z", "+00:00")) + dt.timedelta(seconds=seconds)).isoformat()
    except ValueError:
        return s


def attach_park_ride(city: City, itineraries: list[dict], zones: list[dict], policies: list[dict], *,
                     when: dt.datetime | None = None, locale: str | None = None) -> list[dict]:
    """Turn CAR_DROP_OFF itineraries into park & ride ones; drop those with nowhere legal to leave the car.

    For each itinerary whose car leg feeds transit: pick the parking zone (see `find_parking`) around the
    drop-off, point the car leg at it, replace OTP's drop-off walk with a walk from the zone to the stop, and
    if that walk is longer, leave earlier by the difference so the same bus is still caught. The itinerary
    gets a `parking` block and its fare a `parking` line for the city's default dwell."""
    pr = city.open_mobility.park_ride
    when = when or city_now(city)
    by_id = {str(p["curb_policy_id"]): p for p in policies}
    out: list[dict] = []
    for it in itineraries:
        legs = it.get("legs") or []
        idx = next((i for i, lg in enumerate(legs) if lg.get("mode") == "CAR" and not lg.get("transit")), None)
        if idx is None or not any(lg.get("transit") for lg in legs[idx + 1:]):
            continue
        car = legs[idx]
        if (car.get("distanceMeters") or 0) > pr.max_drive_km * 1000:
            continue
        drop = car.get("to") or {}
        if drop.get("lat") is None or drop.get("lon") is None:
            continue
        found = find_parking(zones, by_id, drop["lat"], drop["lon"], when=when, city=city,
                             max_walk_m=pr.max_walk_meters, locale=locale)
        if not found:
            continue
        zone, view = found["zone"], found["view"]
        center = view.get("center") or {"lat": drop["lat"], "lon": drop["lon"]}

        # the stop the walk must reach is where OTP's own drop-off walk ended (or the drop-off itself)
        nxt = legs[idx + 1] if idx + 1 < len(legs) else None
        otp_walk = nxt if nxt and nxt.get("mode") == "WALK" and not nxt.get("transit") else None
        stop = (otp_walk or car).get("to") or drop
        walk_m = round(_haversine(center["lat"], center["lon"], stop["lat"], stop["lon"]) * WALK_DETOUR)
        walk_s = int(round(walk_m / WALK_SPEED_MPS))
        old_walk_s = int(otp_walk.get("durationSeconds") or 0) if otp_walk else 0
        delta = max(0, walk_s - old_walk_s)

        car["to"] = {**drop, "name": zone.get("name") or drop.get("name"), "lat": center["lat"],
                     "lon": center["lon"], "stopId": None, "stopCode": None}
        car["parkRide"] = True
        walk_end = (otp_walk or car).get("endTime")
        walk = {
            "mode": "WALK", "transit": False,
            "startTime": _shift(walk_end, -walk_s), "endTime": walk_end,
            "durationSeconds": walk_s, "distanceMeters": float(walk_m),
            "from": {"name": zone.get("name"), "lat": center["lat"], "lon": center["lon"]},
            "to": dict(stop),
            "route": None, "headsign": None, "agency": None, "tripId": None, "realtime": False,
            "realtimeState": None, "delaySeconds": None,
            "geometry": {"encoded": encode_polyline([(center["lon"], center["lat"]), (stop["lon"], stop["lat"])]),
                         "precision": 5},
            "intermediateStops": [], "steps": [], "alerts": [], "rental": None,
        }
        car["endTime"] = walk["startTime"]
        if delta:
            car["startTime"] = _shift(car.get("startTime"), -delta)
            for lg in legs[:idx]:
                lg["startTime"], lg["endTime"] = _shift(lg.get("startTime"), -delta), _shift(lg.get("endTime"), -delta)
            it["startTime"] = _shift(it.get("startTime"), -delta)
            it["durationSeconds"] = int(it.get("durationSeconds") or 0) + delta
        it["legs"] = legs[:idx + 1] + [walk] + legs[idx + (2 if otp_walk else 1):]
        old_walk_m = float(otp_walk.get("distanceMeters") or 0) if otp_walk else 0.0
        it["walkDistanceMeters"] = round(float(it.get("walkDistanceMeters") or 0) - old_walk_m + walk_m, 1)
        it["walkTimeSeconds"] = int(it.get("walkTimeSeconds") or 0) - old_walk_s + walk_s

        rules = _winning_rules(zone, by_id, when, city)
        fee = parking_fee(rules, pr.default_dwell_hours)
        it["parking"] = {
            "curbZoneId": view["id"], "name": zone.get("name"), "streetName": zone.get("street_name"),
            "lat": center["lat"], "lon": center["lon"],
            "availableSpaces": view.get("availableSpaces"), "totalSpaces": view.get("totalSpaces"),
            "availabilityTime": view.get("availabilityTime"),
            "priceLabel": view.get("priceLabel"), "whyLegal": view.get("whyLegal"),
            "allowedUntil": view.get("nextChange"),
            "fee": None if fee is None else {"amount": fee, "currency": city.rate_currency(),
                                             "dwellHours": pr.default_dwell_hours, "estimated": True},
            "walkMeters": walk_m, "walkSeconds": walk_s,
        }
        it["source"] = "parkride"
        it["modesUsed"] = [m for m in it.get("modesUsed", []) if m != "CAR_ONDEMAND"]
        if "CAR" not in it["modesUsed"]:
            it["modesUsed"].insert(0, "CAR")
        from .features import estimate_fare
        it["fare"] = estimate_fare(city, it["legs"], locale or city.locale, parking=it["parking"])
        out.append(it)
    return out


def merge_park_ride(chosen: list[dict], park: list[dict], num: int, *, max_combos: int = 2) -> list[dict]:
    """Add park & ride itineraries next to the transit ones: the best transit itinerary is never displaced,
    at most `max_combos` (shortest first) are added under a cap of `num + 3`, the list is re-sorted by
    arrival and re-numbered — the same rules the taxi combos follow."""
    park = sorted(park, key=lambda it: it.get("durationSeconds") or 0)[:max_combos]
    cap = num + 3
    for it in park:
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
