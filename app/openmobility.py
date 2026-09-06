"""
Open Mobility Foundation interoperability (v1.6, phase A): CDS 1.1.0 Curbs and MDS 2.1.0 Policy/Geography.

Two roles, both per city and both optional:

* **Publisher** — the city serves its own curb inventory (CDS Curbs) and its own rules (MDS Policy +
  Geography) so operators and third-party apps can consume the city as the canonical authority.
* **Consumer** — the city ingests a third-party CDS Curbs feed and/or an MDS Policy/Geography authority.

Everything here belongs to the **open data plane**: curb zones, curb policies, geographies and policies
describe *public regulation*, never a person and never an operator's trips. The restricted plane
(MDS Provider trips/telemetry, CDS Events) is phase B and lives in its own tables; nothing in this module
reads or writes it, so a public response can never leak it.

Time is evaluated in the **city's** timezone, not the server's, and the holiday calendar is the one the
tariff engine already uses (python-holidays, by the city's country).
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import logging
import uuid
from collections.abc import Iterable
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from .cities import City
from .db import pool
from .errors import ApiError
from .geo import haversine_m
from .tariff import is_holiday, point_in_polygon

log = logging.getLogger("ot.openmobility")

CDS_VERSION = "1.1.0"
MDS_VERSION = "2.1.0"
MEDIA_CDS = "application/vnd.cds+json;version=1.1"
MEDIA_MDS = "application/vnd.mds+json;version=2.1"

# CDS 1.1.0 enums (curbs/README.md)
ACTIVITIES = ("parking", "loading", "unloading", "stopping", "travel",
              "no parking", "no loading", "no unloading", "no stopping", "no travel")
USER_CLASSES = ("bicycle", "bus", "cargo_bicycle", "car", "moped", "motorcycle", "scooter", "shuttle", "truck",
                "van", "accessible", "autonomous", "combustion", "electric", "electric_assist", "human",
                # CDS lets an agency use its own strings too; these are the ones our clients colour-code
                "taxi", "rideshare", "delivery", "disabled")
DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
TIME_UNITS = ("second", "minute", "hour", "day", "week", "month", "quarter", "year")

# our normalised zone types (MDS policy rules -> something an app can draw and explain)
ZONE_TYPES = ("no_ride", "no_parking", "speed_limit", "cap", "preferred_parking", "other")


class OpenMobilityError(ApiError):
    status, code = 404, "OPEN_MOBILITY_DISABLED"


class CurbNotFound(ApiError):
    status, code = 404, "CURB_ZONE_NOT_FOUND"


# ------------------------------------------------------------------ time helpers
def ms(t: dt.datetime) -> int:
    """CDS/MDS timestamps are integer milliseconds since the Unix epoch."""
    return int(t.timestamp() * 1000)


def from_ms(v: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(int(v) / 1000, dt.UTC)
    except (TypeError, ValueError):
        return None


def city_now(city: City, at: str | None = None) -> dt.datetime:
    """`at` (ISO-8601, with or without offset) or now, always as an aware datetime in the city's zone."""
    tz = ZoneInfo(city.timezone)
    if not at:
        return dt.datetime.now(tz)
    try:
        parsed = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("at: expected an ISO-8601 datetime", status=422) from None
    return parsed.astimezone(tz) if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _hm(value: str | None) -> int | None:
    """"HH:MM" -> minutes since midnight."""
    if not value:
        return None
    try:
        h, m = value.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _fmt_hm(minutes: int) -> str:
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def time_span_active(span: dict, when: dt.datetime, country: str) -> bool:
    """CDS 1.1.0 TimeSpan: every present field must match. An empty span is always active.

    `time_of_day_start` is inclusive and `time_of_day_end` exclusive; a window whose start is after its end
    (22:00–06:00) wraps past midnight.
    """
    if not span:
        return True
    start = from_ms(span.get("start_date"))
    if start and when < start.astimezone(when.tzinfo):
        return False
    end = from_ms(span.get("end_date"))
    if end and when >= end.astimezone(when.tzinfo):
        return False

    dow = span.get("days_of_week")
    if dow and DAYS[(when.weekday() + 1) % 7] not in dow:
        return False
    dom = span.get("days_of_month")
    if dom and when.day not in dom:
        return False
    wom = span.get("weeks_of_month")
    if wom and ((when.day - 1) // 7) + 1 not in wom:
        return False
    months = span.get("months")
    if months and when.month not in months:
        return False

    period = span.get("designated_period")
    if period:
        inside = _designated_period(period, when, country)
        if inside is not None and inside == bool(span.get("designated_period_except")):
            return False

    lo, hi = _hm(span.get("time_of_day_start")), _hm(span.get("time_of_day_end"))
    if lo is None and hi is None:
        return True
    now_m = when.hour * 60 + when.minute
    lo = 0 if lo is None else lo
    hi = 24 * 60 if hi is None else hi
    if lo <= hi:
        return lo <= now_m < hi
    return now_m >= lo or now_m < hi          # wraps midnight


def _designated_period(period: str, when: dt.datetime, country: str) -> bool | None:
    """Only `holidays` can be answered from data we hold; anything else is unknown (never blocks)."""
    if period.strip().lower() in ("holidays", "holiday"):
        return is_holiday(country, when.date())
    return None


def spans_active(spans: list[dict] | None, when: dt.datetime, country: str) -> bool:
    """A policy applies when ANY of its time spans matches (CDS: no spans = always)."""
    if not spans:
        return True
    return any(time_span_active(s, when, country) for s in spans)


def _boundaries(spans: list[dict] | None, when: dt.datetime, days: int = 8) -> list[dt.datetime]:
    """Candidate instants where a span's truth value can flip: every time-of-day edge of the next `days`
    days plus each midnight. Exact (spans only change on those edges) and far cheaper than stepping."""
    edges: set[int] = {0}
    for s in spans or []:
        for key in ("time_of_day_start", "time_of_day_end"):
            m = _hm(s.get(key))
            if m is not None:
                edges.add(m)
    out: list[dt.datetime] = []
    midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(days):
        day = midnight + dt.timedelta(days=d)
        for m in sorted(edges):
            t = day + dt.timedelta(minutes=m)
            if t > when:
                out.append(t)
    return sorted(out)


def next_change(spans: list[dict] | None, when: dt.datetime, country: str) -> dt.datetime | None:
    """When the active/inactive state of this policy flips next (within 8 days), or None."""
    state = spans_active(spans, when, country)
    for t in _boundaries(spans, when):
        if spans_active(spans, t, country) != state:
            return t
    return None


# ------------------------------------------------------------------ geometry helpers
def geometry_coords(geom: dict | None) -> Iterable[tuple[float, float]]:
    """Every (lon, lat) pair of a GeoJSON geometry, whatever its type."""
    if not isinstance(geom, dict):
        return
    if geom.get("type") == "GeometryCollection":
        for g in geom.get("geometries") or []:
            yield from geometry_coords(g)
        return
    yield from _coords(geom.get("coordinates"))


def _coords(node: Any) -> Iterable[tuple[float, float]]:
    if isinstance(node, list | tuple):
        if len(node) >= 2 and all(isinstance(x, int | float) for x in node[:2]):
            yield float(node[0]), float(node[1])
        else:
            for child in node:
                yield from _coords(child)


def geometry_bbox(geom: dict | None) -> tuple[float, float, float, float] | None:
    pts = list(geometry_coords(geom))
    if not pts:
        return None
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


def outer_rings(geom: dict | None) -> list[list[list[float]]]:
    """Outer rings of a Polygon / MultiPolygon (for point-in-polygon)."""
    if not isinstance(geom, dict):
        return []
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Polygon" and c:
        return [c[0]]
    if t == "MultiPolygon" and c:
        return [poly[0] for poly in c if poly]
    if t == "GeometryCollection":
        out: list[list[list[float]]] = []
        for g in geom.get("geometries") or []:
            out.extend(outer_rings(g))
        return out
    return []


def contains_point(geom: dict | None, lon: float, lat: float) -> bool:
    return any(point_in_polygon(lon, lat, ring) for ring in outer_rings(geom))


def distance_to_geometry_m(geom: dict | None, lon: float, lat: float) -> float | None:
    """0 inside a polygon, else the distance to the nearest vertex. Good enough to rank curb zones,
    which are a few metres of kerb; documented as an approximation."""
    if contains_point(geom, lon, lat):
        return 0.0
    best: float | None = None
    for glon, glat in geometry_coords(geom):
        d = haversine_m(lat, lon, glat, glon)
        best = d if best is None else min(best, d)
    return best


def bbox_overlaps(box: tuple[float, float, float, float] | None,
                  other: tuple[float, float, float, float] | None) -> bool:
    if box is None or other is None:
        return True
    return not (other[2] < box[0] or other[0] > box[2] or other[3] < box[1] or other[1] > box[3])


def _uuid_from(text: str) -> str:
    """Stable UUID for a caller-supplied id that is not a UUID (CDS/MDS ids must be UUIDs)."""
    return str(uuid.UUID(bytes=hashlib.sha256(text.encode()).digest()[:16], version=5))


def ensure_uuid(value: Any, *, seed: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return _uuid_from(seed)


# ------------------------------------------------------------------ normalisation for our own clients
_ACTIVITY_LABEL_ES = {"parking": "Estacionamiento", "loading": "Zona de cargue", "unloading": "Zona de descargue",
                      "stopping": "Parada breve", "travel": "Circulación",
                      "no parking": "Prohibido estacionar", "no loading": "Prohibido cargar",
                      "no unloading": "Prohibido descargar", "no stopping": "Prohibido detenerse",
                      "no travel": "Prohibido circular"}
_ACTIVITY_LABEL_EN = {"parking": "Parking", "loading": "Loading zone", "unloading": "Unloading zone",
                      "stopping": "Standing", "travel": "Travel", "no parking": "No parking",
                      "no loading": "No loading", "no unloading": "No unloading",
                      "no stopping": "No stopping", "no travel": "No travel"}
_DAY_ES = {"mon": "Lun", "tue": "Mar", "wed": "Mié", "thu": "Jue", "fri": "Vie", "sat": "Sáb", "sun": "Dom"}
_DAY_EN = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}

# a curb rule allows a rider pick-up / drop-off when it permits one of these
_PICKUP_ACTIVITIES = ("stopping", "loading", "unloading", "parking")
# user class synonyms so a zone written for `car` still matches a rideshare request
_USER_CLASS_SYNONYMS = {
    "car": {"car", "rideshare", "taxi", "autonomous", "combustion", "electric"},
    "rideshare": {"rideshare", "car", "taxi", "autonomous"},
    "taxi": {"taxi", "car", "rideshare"},
    "delivery": {"delivery", "van", "truck", "cargo_bicycle", "car"},
    "disabled": {"disabled", "accessible"},
    "bicycle": {"bicycle", "cargo_bicycle", "electric_assist"},
    "scooter": {"scooter", "moped"},
}


_RATE_UNIT_ES = {"second": "segundo", "minute": "minuto", "hour": "hora", "day": "día", "week": "semana",
                 "month": "mes", "quarter": "trimestre", "year": "año"}
_RATE_UNIT_EN = {u: u for u in TIME_UNITS}


def format_amount(minor: float, currency: str, minor_units: int, locale: str) -> str:
    """A CDS `rate` is an integer in the smallest denomination of the local currency; `minor_units` says how
    many of those make one unit (COP quotes whole pesos -> 1; USD/EUR quote cents -> 100)."""
    value = minor / minor_units if minor_units else minor
    if minor_units == 1:
        body = f"{int(round(value)):,}".replace(",", ".")
    else:
        body = f"{value:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")
    symbol = "$" if currency in ("COP", "USD", "MXN", "CLP", "ARS") else ""
    return (f"{symbol} {body}" if symbol else f"{body} {currency}") if not (locale or "es").startswith("en") \
        else (f"{symbol}{body}" if symbol else f"{body} {currency}")


def price_label(rules: list[dict], city: City, locale: str | None = None) -> str | None:
    """"$ 4.200 / hora · máx 2 h" — the human price of a paid-parking rule."""
    locale = locale or city.locale
    currency = city.rate_currency()
    minor = city.open_mobility.cds.rate_minor_units or 1
    units = _RATE_UNIT_EN if (locale or "es").startswith("en") else _RATE_UNIT_ES
    parts: list[str] = []
    for r in rules:
        for rate in r.get("rate") or []:
            amount = rate.get("rate")
            if amount is None:
                continue
            if amount == 0:
                parts.append("Gratis" if not (locale or "es").startswith("en") else "Free")
                continue
            unit = units.get(rate.get("rate_unit", "hour"), rate.get("rate_unit", "hour"))
            chunk = f"{format_amount(amount, currency, minor, locale)} / {unit}"
            fee = rate.get("maximum_fee")
            if fee:
                cap = "máx" if not (locale or "es").startswith("en") else "max"
                chunk += f" ({cap} {format_amount(fee, currency, minor, locale)})"
            parts.append(chunk)
    stay = next(((r.get("max_stay"), r.get("max_stay_unit") or "minute") for r in rules if r.get("max_stay")),
                None)
    if stay:
        unit = units.get(stay[1], stay[1])
        parts.append((f"máx {stay[0]} {unit}") if not (locale or "es").startswith("en")
                     else f"max {stay[0]} {unit}")
    return " · ".join(parts) or None


def _label(activity: str, locale: str) -> str:
    table = _ACTIVITY_LABEL_EN if (locale or "es").startswith("en") else _ACTIVITY_LABEL_ES
    return table.get(activity, activity)


def rule_matches_user_class(rule: dict, user_class: str | None) -> bool:
    """CDS: `user_classes` limits a rule to those classes; `user_classes_except` excludes them."""
    if not user_class:
        return True
    wanted = _USER_CLASS_SYNONYMS.get(user_class, {user_class})
    only = rule.get("user_classes")
    if only and not (set(only) & wanted):
        return False
    never = rule.get("user_classes_except")
    if never and (set(never) & wanted):
        return False
    return True


def describe_spans(spans: list[dict] | None, locale: str) -> str:
    """"Lun–Sáb 06:00–18:00" — the human half of `whyLegal`."""
    if not spans:
        return "24/7"
    days_tbl = _DAY_EN if (locale or "es").startswith("en") else _DAY_ES
    parts: list[str] = []
    for s in spans:
        chunk = []
        dows = s.get("days_of_week")
        if dows:
            chunk.append(" ".join(days_tbl.get(d, d) for d in dows))
        lo, hi = s.get("time_of_day_start"), s.get("time_of_day_end")
        if lo or hi:
            chunk.append(f"{lo or '00:00'}–{hi or '24:00'}")
        if s.get("designated_period"):
            chunk.append(str(s["designated_period"]))
        parts.append(" ".join(chunk) if chunk else "24/7")
    return " · ".join(parts)


def evaluate_zone(zone: dict, policies: dict[str, dict], when: dt.datetime, city: City,
                  *, user_class: str | None = None, locale: str | None = None) -> dict:
    """Which of a zone's policies apply right now, and what that means for a pick-up.

    CDS orders overlapping policies by `priority`, **lowest wins**. We keep every active policy (an app may
    want to show them all) but the decision fields follow the winner.
    """
    locale = locale or city.locale
    country = city.country
    active: list[dict] = []
    for pid in zone.get("curb_policy_ids") or []:
        pol = policies.get(str(pid))
        if not pol:
            continue
        if not spans_active(pol.get("time_spans"), when, country):
            continue
        rules = [r for r in (pol.get("rules") or []) if rule_matches_user_class(r, user_class)]
        if user_class and not rules:
            continue
        active.append({"policy": pol, "rules": rules})
    active.sort(key=lambda a: (a["policy"].get("priority", 0), str(a["policy"].get("curb_policy_id"))))

    allowed: bool | None = None
    why = ""
    winner = active[0] if active else None
    if winner:
        acts = [r.get("activity") for r in winner["rules"]]
        positive = [a for a in acts if a in _PICKUP_ACTIVITIES]
        negative = [a for a in acts if a and a.startswith("no ")]
        allowed = bool(positive) and not negative
        head = _label(positive[0] if positive else (negative[0] if negative else "parking"), locale)
        why = f"{head} · {describe_spans(winner['policy'].get('time_spans'), locale)}"
        price = price_label(winner["rules"], city, locale)
        if price and allowed:
            why += f" · {price}"

    change = None
    for a in active or [{"policy": policies.get(str(p), {})} for p in (zone.get("curb_policy_ids") or [])]:
        t = next_change((a["policy"] or {}).get("time_spans"), when, country)
        if t and (change is None or t < change):
            change = t
    return {"allowed": allowed, "whyLegal": why or None,
            "nextChange": change.isoformat() if change else None,
            "activePolicyIds": [str(a["policy"].get("curb_policy_id")) for a in active]}


def curb_public(zone: dict, policies: dict[str, dict], when: dt.datetime, city: City,
                *, user_class: str | None = None, lat: float | None = None, lon: float | None = None) -> dict:
    """Our camelCase view of a CDS curb zone (the verbatim CDS object stays available under `cds`)."""
    ev = evaluate_zone(zone, policies, when, city, user_class=user_class)
    geom = zone.get("geometry")
    out = {
        "id": str(zone.get("curb_zone_id")),
        "name": zone.get("name"),
        "streetName": zone.get("street_name"),
        "geometry": geom,
        "center": _center(geom),
        "length": zone.get("length"),
        "width": zone.get("width"),
        "availableSpaces": zone.get("available_spaces"),
        "available": zone.get("available"),
        "availableSpaceLengths": zone.get("available_space_lengths"),
        "availabilityTime": (lambda t: t.isoformat() if t else None)(from_ms(zone.get("availability_time"))),
        "priceLabel": _winning_price_label(zone, policies, when, city, user_class=user_class),
        "policies": [_policy_public(policies[str(p)], city)
                     for p in (zone.get("curb_policy_ids") or []) if str(p) in policies],
        **ev,
    }
    if lat is not None and lon is not None:
        d = distance_to_geometry_m(geom, lon, lat)
        out["distanceMeters"] = round(d) if d is not None else None
    return out


def _winning_price_label(zone: dict, policies: dict[str, dict], when: dt.datetime, city: City,
                         *, user_class: str | None) -> str | None:
    """Price of the policy that actually wins right now (CDS: lowest `priority` first)."""
    active = []
    for pid in zone.get("curb_policy_ids") or []:
        pol = policies.get(str(pid))
        if pol and spans_active(pol.get("time_spans"), when, city.country):
            rules = [r for r in (pol.get("rules") or []) if rule_matches_user_class(r, user_class)]
            if rules:
                active.append((pol.get("priority", 0), str(pol.get("curb_policy_id")), rules))
    if not active:
        return None
    active.sort()
    return price_label(active[0][2], city)


def _center(geom: dict | None) -> dict | None:
    pts = list(geometry_coords(geom))
    if not pts:
        return None
    return {"lat": round(sum(p[1] for p in pts) / len(pts), 6),
            "lon": round(sum(p[0] for p in pts) / len(pts), 6)}


def _policy_public(pol: dict, city: City) -> dict:
    return {"id": str(pol.get("curb_policy_id")), "name": pol.get("name"),
            "priority": pol.get("priority"),
            "rules": [{"activity": r.get("activity"), "maxStay": r.get("max_stay"),
                       "maxStayUnit": r.get("max_stay_unit"), "userClasses": r.get("user_classes"),
                       "userClassesExcept": r.get("user_classes_except"), "rate": r.get("rate")}
                      for r in (pol.get("rules") or [])],
            "priceLabel": price_label(pol.get("rules") or [], city),
            "timeSpans": pol.get("time_spans") or [],
            "describes": describe_spans(pol.get("time_spans"), city.locale)}


# ------------------------------------------------------------------ MDS policy/geography -> our zones
def _rule_zone_type(rule: dict) -> str:
    """MDS 2.1 rules are generic; map the shapes an app can actually draw."""
    kind = rule.get("rule_type")
    states = rule.get("states") or {}
    maximum, minimum = rule.get("maximum"), rule.get("minimum")
    if kind == "speed":
        return "speed_limit"
    if kind == "count":
        if maximum == 0:
            return "no_ride" if "on_trip" in states else "no_parking"
        if minimum and minimum > 0 and not maximum:
            return "preferred_parking"
        return "cap"
    if kind == "time" and maximum == 0:
        return "no_ride"
    return "other"


def _rule_time_spans(rule: dict) -> list[dict]:
    """MDS puts days/start_time/end_time on the rule; express them as CDS-shaped spans so the clients
    (and `next_change`) have one time model to reason about."""
    if not (rule.get("days") or rule.get("start_time") or rule.get("end_time")):
        return []
    span: dict = {}
    if rule.get("days"):
        span["days_of_week"] = rule["days"]
    if rule.get("start_time"):
        span["time_of_day_start"] = str(rule["start_time"])[:5]
    if rule.get("end_time"):
        span["time_of_day_end"] = str(rule["end_time"])[:5]
    return [span]


def _geometry_of(geographies: dict[str, dict], ids: list[str]) -> dict | None:
    """Merge the referenced MDS geographies (each a GeoJSON FeatureCollection) into one geometry."""
    geoms: list[dict] = []
    for gid in ids or []:
        g = geographies.get(str(gid))
        if not g:
            continue
        gj = g.get("geography_json") or {}
        if gj.get("type") == "FeatureCollection":
            geoms.extend([f.get("geometry") for f in gj.get("features") or [] if f.get("geometry")])
        elif gj.get("type") == "Feature" and gj.get("geometry"):
            geoms.append(gj["geometry"])
        elif gj.get("type"):
            geoms.append(gj)
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    if all(g.get("type") == "Polygon" for g in geoms):
        return {"type": "MultiPolygon", "coordinates": [g["coordinates"] for g in geoms]}
    return {"type": "GeometryCollection", "geometries": geoms}


def zones_public(policies: list[dict], geographies: list[dict], when: dt.datetime, city: City) -> list[dict]:
    """MDS Policy rules + the geographies they reference -> the flat `/zones` shape the apps draw."""
    geo_by_id = {str(g.get("geography_id")): g for g in geographies}
    out: list[dict] = []
    for pol in policies:
        start, end = from_ms(pol.get("start_date")), from_ms(pol.get("end_date"))
        if start and when < start.astimezone(when.tzinfo):
            continue
        if end and when >= end.astimezone(when.tzinfo):
            continue
        for rule in pol.get("rules") or []:
            spans = _rule_time_spans(rule)
            geom = _geometry_of(geo_by_id, rule.get("geographies") or [])
            if geom is None:
                continue
            change = next_change(spans, when, city.country)
            out.append({
                "id": str(rule.get("rule_id")),
                "policyId": str(pol.get("policy_id")),
                "name": rule.get("name") or pol.get("name"),
                "description": pol.get("description"),
                "type": _rule_zone_type(rule),
                "rule": {"ruleType": rule.get("rule_type"), "ruleUnits": rule.get("rule_units"),
                         "minimum": rule.get("minimum"), "maximum": rule.get("maximum"),
                         "states": rule.get("states"), "messages": rule.get("messages")},
                "appliesTo": [pol.get("mode_id") or "micromobility"],
                "vehicleTypes": rule.get("vehicle_types") or [],
                "timeSpans": spans,
                "active": spans_active(spans, when, city.country),
                "nextChange": change.isoformat() if change else None,
                "geometry": geom,
                "geographyIds": [str(g) for g in rule.get("geographies") or []],
            })
    return out


def geographies_public(geographies: list[dict], when: dt.datetime) -> list[dict]:
    out = []
    for g in geographies:
        eff = from_ms(g.get("effective_date"))
        retire = from_ms(g.get("retire_date"))
        if eff and when < eff.astimezone(when.tzinfo):
            continue
        if retire and when >= retire.astimezone(when.tzinfo):
            continue
        out.append({"id": str(g.get("geography_id")), "name": g.get("name"),
                    "description": g.get("description"), "type": g.get("geography_type"),
                    "geometry": _geometry_of({str(g.get("geography_id")): g}, [str(g.get("geography_id"))])})
    return out


# ------------------------------------------------------------------ import (CDS document or plain GeoJSON)
def parse_curbs_document(doc: Any) -> tuple[list[dict], list[dict]]:
    """Accept a CDS Curbs response (`{data: {zones, policies}}` or the bare arrays) **or** a GeoJSON
    FeatureCollection whose properties carry the CDS fields. Returns (zones, policies)."""
    if not isinstance(doc, dict):
        raise ApiError("expected a JSON object", status=422)
    if doc.get("type") == "FeatureCollection":
        return _from_geojson(doc)
    data = doc.get("data") if isinstance(doc.get("data"), dict) else doc
    zones = data.get("zones") or data.get("curb_zones") or []
    policies = data.get("policies") or data.get("curb_policies") or []
    if not isinstance(zones, list) or not isinstance(policies, list):
        raise ApiError("zones and policies must be arrays", status=422)
    return [_norm_zone(z) for z in zones], [_norm_policy(p) for p in policies]


def _from_geojson(fc: dict) -> tuple[list[dict], list[dict]]:
    zones, policies = [], []
    for i, feat in enumerate(fc.get("features") or []):
        props = dict(feat.get("properties") or {})
        geom = feat.get("geometry")
        if not geom:
            continue
        seed = str(props.get("id") or props.get("curb_zone_id") or props.get("name") or f"zone-{i}")
        zid = ensure_uuid(props.get("curb_zone_id") or props.get("id"), seed=seed)
        inline = props.get("policies") or props.get("curb_policies")
        pids: list[str] = []
        if isinstance(inline, list):
            for j, p in enumerate(inline):
                pol = _norm_policy(p, seed=f"{seed}-policy-{j}")
                policies.append(pol)
                pids.append(pol["curb_policy_id"])
        else:
            pids = [ensure_uuid(p, seed=f"{seed}-{p}") for p in (props.get("curb_policy_ids") or [])]
        zones.append(_norm_zone({**props, "curb_zone_id": zid, "geometry": geom, "curb_policy_ids": pids},
                                seed=seed))
    return zones, policies


def _now_ms() -> int:
    return ms(dt.datetime.now(dt.UTC))


def _norm_zone(z: dict, *, seed: str | None = None) -> dict:
    if not isinstance(z, dict) or not z.get("geometry"):
        raise ApiError("every curb zone needs a GeoJSON `geometry`", status=422)
    seed = seed or str(z.get("curb_zone_id") or z.get("name") or json.dumps(z.get("geometry"))[:120])
    out = dict(z)
    out["curb_zone_id"] = ensure_uuid(z.get("curb_zone_id"), seed=seed)
    out["curb_policy_ids"] = [str(p) for p in (z.get("curb_policy_ids") or [])]
    now = _now_ms()
    out.setdefault("published_date", now)
    out["last_updated_date"] = now
    out.setdefault("start_date", now)
    return out


def _norm_policy(p: dict, *, seed: str | None = None) -> dict:
    if not isinstance(p, dict):
        raise ApiError("every curb policy must be an object", status=422)
    rules = p.get("rules") or []
    if not rules:
        raise ApiError("every curb policy needs at least one rule", status=422)
    for r in rules:
        if r.get("activity") not in ACTIVITIES:
            raise ApiError(f"rules.activity: expected one of {', '.join(ACTIVITIES)}", status=422)
    seed = seed or str(p.get("curb_policy_id") or json.dumps(rules)[:120])
    out = dict(p)
    out["curb_policy_id"] = ensure_uuid(p.get("curb_policy_id"), seed=seed)
    out.setdefault("priority", 1)
    out.setdefault("published_date", _now_ms())
    out["time_spans"] = p.get("time_spans") or []
    return out


def parse_mds_documents(doc: Any) -> tuple[list[dict], list[dict]]:
    """Accept an MDS Policy response, a Geography response, or a `{policies, geographies}` bundle."""
    if not isinstance(doc, dict):
        raise ApiError("expected a JSON object", status=422)
    data = doc.get("data") if isinstance(doc.get("data"), dict) else doc
    policies = data.get("policies") or []
    geographies = data.get("geographies") or []
    if doc.get("policy"):
        policies = [doc["policy"]]
    if doc.get("geography"):
        geographies = [doc["geography"]]
    if not isinstance(policies, list) or not isinstance(geographies, list):
        raise ApiError("policies and geographies must be arrays", status=422)
    return [_norm_mds_policy(p) for p in policies], [_norm_mds_geography(g) for g in geographies]


def _norm_mds_policy(p: dict) -> dict:
    if not isinstance(p, dict) or not p.get("rules"):
        raise ApiError("every MDS policy needs `rules`", status=422)
    out = dict(p)
    out["policy_id"] = ensure_uuid(p.get("policy_id"), seed=str(p.get("name") or json.dumps(p["rules"])[:120]))
    out.setdefault("published_date", _now_ms())
    out.setdefault("start_date", _now_ms())
    for i, r in enumerate(out["rules"]):
        r["rule_id"] = ensure_uuid(r.get("rule_id"), seed=f"{out['policy_id']}-rule-{i}")
        r["geographies"] = [str(g) for g in (r.get("geographies") or [])]
    return out


def _norm_mds_geography(g: dict) -> dict:
    if not isinstance(g, dict) or not g.get("geography_json"):
        raise ApiError("every MDS geography needs `geography_json`", status=422)
    out = dict(g)
    out["geography_id"] = ensure_uuid(g.get("geography_id"), seed=str(g.get("name") or "geography"))
    out.setdefault("published_date", _now_ms())
    return out


# ------------------------------------------------------------------ storage
class OpenMobilityStore(Protocol):
    async def put_curbs(self, city_id: str, zones: list[dict], policies: list[dict], *,
                        replace: bool) -> dict: ...
    async def curbs(self, city_id: str) -> tuple[list[dict], list[dict]]: ...
    async def delete_curb(self, city_id: str, zone_id: str) -> bool: ...
    async def clear_curbs(self, city_id: str) -> int: ...
    async def put_mds(self, city_id: str, policies: list[dict], geographies: list[dict], *,
                      replace: bool) -> dict: ...
    async def mds(self, city_id: str) -> tuple[list[dict], list[dict]]: ...
    async def stats(self, city_id: str) -> dict: ...


class MemoryOpenMobilityStore:
    """In-memory store (tests and dev without a database)."""

    def __init__(self) -> None:
        self.zones: dict[str, dict[str, dict]] = {}
        self.policies: dict[str, dict[str, dict]] = {}
        self.mds_policies: dict[str, dict[str, dict]] = {}
        self.geographies: dict[str, dict[str, dict]] = {}
        self.updated: dict[str, dt.datetime] = {}

    async def put_curbs(self, city_id: str, zones, policies, *, replace: bool) -> dict:
        if replace:
            self.zones[city_id] = {}
            self.policies[city_id] = {}
        z = self.zones.setdefault(city_id, {})
        p = self.policies.setdefault(city_id, {})
        for item in zones:
            z[str(item["curb_zone_id"])] = copy.deepcopy(item)
        for item in policies:
            p[str(item["curb_policy_id"])] = copy.deepcopy(item)
        self.updated[city_id] = dt.datetime.now(dt.UTC)
        return {"zones": len(zones), "policies": len(policies)}

    async def curbs(self, city_id: str) -> tuple[list[dict], list[dict]]:
        return (copy.deepcopy(list(self.zones.get(city_id, {}).values())),
                copy.deepcopy(list(self.policies.get(city_id, {}).values())))

    async def delete_curb(self, city_id: str, zone_id: str) -> bool:
        return self.zones.get(city_id, {}).pop(zone_id, None) is not None

    async def clear_curbs(self, city_id: str) -> int:
        n = len(self.zones.get(city_id, {}))
        self.zones[city_id] = {}
        self.policies[city_id] = {}
        return n

    async def put_mds(self, city_id: str, policies, geographies, *, replace: bool) -> dict:
        if replace:
            self.mds_policies[city_id] = {}
            self.geographies[city_id] = {}
        mp = self.mds_policies.setdefault(city_id, {})
        gg = self.geographies.setdefault(city_id, {})
        for item in policies:
            mp[str(item["policy_id"])] = copy.deepcopy(item)
        for item in geographies:
            gg[str(item["geography_id"])] = copy.deepcopy(item)
        self.updated[city_id] = dt.datetime.now(dt.UTC)
        return {"policies": len(policies), "geographies": len(geographies)}

    async def mds(self, city_id: str) -> tuple[list[dict], list[dict]]:
        return (copy.deepcopy(list(self.mds_policies.get(city_id, {}).values())),
                copy.deepcopy(list(self.geographies.get(city_id, {}).values())))

    async def stats(self, city_id: str) -> dict:
        t = self.updated.get(city_id)
        return {"curbZones": len(self.zones.get(city_id, {})),
                "curbPolicies": len(self.policies.get(city_id, {})),
                "mdsPolicies": len(self.mds_policies.get(city_id, {})),
                "mdsGeographies": len(self.geographies.get(city_id, {})),
                "lastUpdatedAt": t.isoformat().replace("+00:00", "Z") if t else None}


class PgOpenMobilityStore:
    async def put_curbs(self, city_id: str, zones, policies, *, replace: bool) -> dict:
        async with pool().acquire() as c, c.transaction():
            if replace:
                await c.execute("DELETE FROM curb_zone WHERE city_id=$1", city_id)
                await c.execute("DELETE FROM curb_policy WHERE city_id=$1", city_id)
            for z in zones:
                box = geometry_bbox(z.get("geometry")) or (0.0, 0.0, 0.0, 0.0)
                await c.execute(
                    """INSERT INTO curb_zone (city_id, curb_zone_id, data, min_lon, min_lat, max_lon, max_lat,
                                              updated_at)
                       VALUES ($1,$2,$3::jsonb,$4,$5,$6,$7, now())
                       ON CONFLICT (city_id, curb_zone_id) DO UPDATE SET data=EXCLUDED.data,
                           min_lon=EXCLUDED.min_lon, min_lat=EXCLUDED.min_lat, max_lon=EXCLUDED.max_lon,
                           max_lat=EXCLUDED.max_lat, updated_at=now()""",
                    city_id, str(z["curb_zone_id"]), json.dumps(z), *box)
            for p in policies:
                await c.execute(
                    """INSERT INTO curb_policy (city_id, curb_policy_id, data, updated_at)
                       VALUES ($1,$2,$3::jsonb, now())
                       ON CONFLICT (city_id, curb_policy_id) DO UPDATE SET data=EXCLUDED.data,
                           updated_at=now()""",
                    city_id, str(p["curb_policy_id"]), json.dumps(p))
        return {"zones": len(zones), "policies": len(policies)}

    async def curbs(self, city_id: str) -> tuple[list[dict], list[dict]]:
        async with pool().acquire() as c:
            zr = await c.fetch("SELECT data FROM curb_zone WHERE city_id=$1", city_id)
            pr = await c.fetch("SELECT data FROM curb_policy WHERE city_id=$1", city_id)
        return [_json(r["data"]) for r in zr], [_json(r["data"]) for r in pr]

    async def delete_curb(self, city_id: str, zone_id: str) -> bool:
        async with pool().acquire() as c:
            res = await c.execute("DELETE FROM curb_zone WHERE city_id=$1 AND curb_zone_id=$2", city_id, zone_id)
        return res.endswith(" 1")

    async def clear_curbs(self, city_id: str) -> int:
        async with pool().acquire() as c, c.transaction():
            n = await c.fetchval("SELECT count(*) FROM curb_zone WHERE city_id=$1", city_id)
            await c.execute("DELETE FROM curb_zone WHERE city_id=$1", city_id)
            await c.execute("DELETE FROM curb_policy WHERE city_id=$1", city_id)
        return int(n or 0)

    async def put_mds(self, city_id: str, policies, geographies, *, replace: bool) -> dict:
        async with pool().acquire() as c, c.transaction():
            if replace:
                await c.execute("DELETE FROM mds_policy WHERE city_id=$1", city_id)
                await c.execute("DELETE FROM mds_geography WHERE city_id=$1", city_id)
            for p in policies:
                await c.execute(
                    """INSERT INTO mds_policy (city_id, policy_id, data, updated_at)
                       VALUES ($1,$2,$3::jsonb, now())
                       ON CONFLICT (city_id, policy_id) DO UPDATE SET data=EXCLUDED.data, updated_at=now()""",
                    city_id, str(p["policy_id"]), json.dumps(p))
            for g in geographies:
                await c.execute(
                    """INSERT INTO mds_geography (city_id, geography_id, data, updated_at)
                       VALUES ($1,$2,$3::jsonb, now())
                       ON CONFLICT (city_id, geography_id) DO UPDATE SET data=EXCLUDED.data, updated_at=now()""",
                    city_id, str(g["geography_id"]), json.dumps(g))
        return {"policies": len(policies), "geographies": len(geographies)}

    async def mds(self, city_id: str) -> tuple[list[dict], list[dict]]:
        async with pool().acquire() as c:
            pr = await c.fetch("SELECT data FROM mds_policy WHERE city_id=$1", city_id)
            gr = await c.fetch("SELECT data FROM mds_geography WHERE city_id=$1", city_id)
        return [_json(r["data"]) for r in pr], [_json(r["data"]) for r in gr]

    async def stats(self, city_id: str) -> dict:
        async with pool().acquire() as c:
            r = await c.fetchrow(
                """SELECT (SELECT count(*) FROM curb_zone WHERE city_id=$1)      AS zones,
                          (SELECT count(*) FROM curb_policy WHERE city_id=$1)    AS policies,
                          (SELECT count(*) FROM mds_policy WHERE city_id=$1)     AS mds_policies,
                          (SELECT count(*) FROM mds_geography WHERE city_id=$1)  AS geographies,
                          (SELECT max(updated_at) FROM curb_zone WHERE city_id=$1) AS updated""", city_id)
        t = r["updated"]
        return {"curbZones": int(r["zones"]), "curbPolicies": int(r["policies"]),
                "mdsPolicies": int(r["mds_policies"]), "mdsGeographies": int(r["geographies"]),
                "lastUpdatedAt": t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") if t else None}


def _json(v: Any) -> dict:
    return v if isinstance(v, dict) else json.loads(v)


# ------------------------------------------------------------------ remote refresh (consumer role)
async def refresh_from_url(store: OpenMobilityStore, city: City, url: str, *, kind: str = "cds") -> dict:
    """Pull a third-party CDS Curbs or MDS Policy/Geography document and replace our copy."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cli:
        accept = MEDIA_CDS if kind == "cds" else MEDIA_MDS
        r = await cli.get(url, headers={"Accept": f"{accept}, application/json"})
        r.raise_for_status()
        doc = r.json()
    if kind == "cds":
        zones, policies = parse_curbs_document(doc)
        return await store.put_curbs(city.id, zones, policies, replace=True)
    policies, geographies = parse_mds_documents(doc)
    return await store.put_mds(city.id, policies, geographies, replace=True)


# ------------------------------------------------------------------ spec envelopes
def cds_envelope(city: City, key: str, items: list[dict], *, next_url: str | None = None) -> dict:
    body = {"version": CDS_VERSION, "time_zone": city.timezone, "last_updated": _now_ms(),
            "currency": (city.fares.currency if city.fares else "USD"),
            "data": {key: items}, "links": {"next": next_url}}
    return body


def mds_envelope(key: str, items: list[dict]) -> dict:
    return {"version": MDS_VERSION, "last_updated": _now_ms(), key: items}


def etag_for(payload: Any) -> str:
    return '"' + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32] + '"'
