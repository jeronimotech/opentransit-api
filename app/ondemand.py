"""
On-demand mobility (v1.4): taxi and ride-hailing providers as planner options.

Provider-agnostic by construction: the list of providers, their hand-off links, credentials and the taxi
tariff all come from the city configuration. This module builds hand-off URLs (injecting credentials server
side), prices rides with the tariff engine, routes cars through OTP, and decorates CAR legs with an
`onDemand` block. Credentials never leave the process unmasked.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import re
import time
from urllib.parse import quote, urlencode

from .cities import City, OnDemandProvider
from .otp import CAR_QUERY
from .tariff import estimate as tariff_estimate

PLACEHOLDER = re.compile(r"\{([A-Za-z][A-Za-z0-9]*)\}")
CREDENTIAL_PLACEHOLDERS = {"clientId", "clientSecret", "apiKey", "partnerId", "token"}
MASK = "••••"
_LABELS = {"es": {"in_app": "Precio en la app", "no_estimate": "Sin estimación"},
           "en": {"in_app": "Price in the app", "no_estimate": "No estimate"}}


def _lb(locale: str) -> dict:
    return _LABELS["en"] if (locale or "es").startswith("en") else _LABELS["es"]


# ------------------------------------------------------------------ credentials masking
def mask_value(v: str | None) -> str | None:
    if not v:
        return v
    return MASK + v[-4:] if len(v) > 4 else MASK


def is_masked(v: str | None) -> bool:
    return bool(v) and v.startswith(MASK)


def mask_credentials(data):
    """Deep copy of an admin/history payload with every `credentials.*` value masked."""
    out = copy.deepcopy(data)
    _walk(out, lambda creds: {k: mask_value(v) for k, v in creds.items()})
    return out


def strip_credentials(data):
    out = copy.deepcopy(data)
    _walk(out, lambda creds: None)
    return out


def _walk(node, fn) -> None:
    if isinstance(node, dict):
        if "credentials" in node and isinstance(node["credentials"], dict):
            new = fn(node["credentials"])
            if new is None:
                del node["credentials"]
            else:
                node["credentials"] = new
        for v in node.values():
            _walk(v, fn)
    elif isinstance(node, list):
        for v in node:
            _walk(v, fn)


def apply_credential_rules(items: list, stored_for) -> None:
    """Shared credential semantics for any list of objects that carries `credentials` (on-demand providers,
    MDS providers): an OMITTED key keeps every stored value, `null` clears them all, a key set to null clears
    that key, and a MASKED value keeps the stored one. New entries get exactly what they send."""
    for item in items:
        if not isinstance(item, dict):
            continue
        stored = stored_for(item.get("id")) or {}
        if "credentials" not in item:
            if stored:
                item["credentials"] = dict(stored)
            continue
        creds = item["credentials"]
        if creds is None:
            item["credentials"] = {}
            continue
        if not isinstance(creds, dict):
            continue
        for k, v in list(creds.items()):
            if v is None:
                del creds[k]
            elif is_masked(v):
                if stored.get(k):
                    creds[k] = stored[k]
                else:
                    del creds[k]


def unmask_open_mobility_patch(patch: dict | None, city: City) -> dict | None:
    """Same rules for `openMobility.mds.providers[]`."""
    if not patch or not isinstance(patch.get("mds"), dict):
        return patch
    providers = patch["mds"].get("providers")
    if not isinstance(providers, list):
        return patch
    out = copy.deepcopy(patch)
    known = {p.id: dict(p.credentials) for p in city.open_mobility.mds.providers}
    apply_credential_rules(out["mds"]["providers"], known.get)
    return out


def unmask_patch(patch_mobility: dict | None, city: City) -> dict | None:
    """Credential rules for a PUT of `mobility.onDemand[]` (the list replaces the stored one, so secrets must be
    carried over explicitly): for a provider the city already knows, an OMITTED `credentials` key keeps every
    stored value; `credentials: null` clears them all; a key set to null clears that key; a MASKED value keeps
    the stored one; any other value is stored as sent. New providers get exactly what they send."""
    if not patch_mobility or not isinstance(patch_mobility.get("onDemand"), list):
        return patch_mobility
    out = copy.deepcopy(patch_mobility)

    def stored_for(pid):
        cur = city.on_demand_provider(pid)
        return dict(cur.credentials) if cur else {}

    apply_credential_rules(out["onDemand"], stored_for)
    return out


# ------------------------------------------------------------------ car duration realism
def is_night(city: City, when: dt.datetime) -> bool:
    """Inside the night window of the city's (first) tariff that defines one."""
    from .tariff import _in_night_window
    for t in city.mobility.taxi_tariffs:
        for s in t.surcharges:
            if s.when.night_from and s.when.night_to:
                return _in_night_window(when, s.when.night_from, s.when.night_to)
    return False


def duration_factor(city: City, when: dt.datetime) -> float:
    pol = city.mobility.on_demand_policy
    return pol.night_duration_factor if is_night(city, when) else pol.duration_factor


def adjusted_route(city: City, route: dict, when: dt.datetime) -> dict:
    """Copy of an OTP car route with the traffic factor applied to its duration."""
    f = duration_factor(city, when)
    out = dict(route)
    out["durationSeconds"] = int(round((route.get("durationSeconds") or 0) * f))
    out["durationFactor"] = f
    return out


# ------------------------------------------------------------------ hand-off links
def _fallback(p: OnDemandProvider, platform: str | None) -> str | None:
    h = p.handoff
    ios, android, web = h.apps.get("ios"), h.apps.get("android"), h.web
    if platform == "ios":
        return ios or web or android
    if platform == "android":
        return android or web or ios
    return web or ios or android


def build_handoff(p: OnDemandProvider, *, from_lat: float, from_lon: float, to_lat: float, to_lon: float,
                  from_name: str | None = None, to_name: str | None = None,
                  platform: str | None = None) -> dict:
    """{url, fallback, kind}. Template placeholders are URL-encoded; credentials are injected here and only
    here. A template that needs a credential the city has not configured yields the fallback, never a broken
    link."""
    fallback = _fallback(p, platform)
    h = p.handoff
    if h.kind == "none":
        return {"url": None, "fallback": fallback, "kind": "none"}
    if h.kind == "url" or not h.template:
        return {"url": fallback, "fallback": fallback, "kind": "url"}
    pickup = {"latitude": from_lat, "longitude": from_lon, "addressLine1": from_name or ""}
    drop = {"latitude": to_lat, "longitude": to_lon, "addressLine1": to_name or ""}
    values = {
        "pickupLat": f"{from_lat:.6f}", "pickupLon": f"{from_lon:.6f}", "pickupName": from_name or "",
        "dropoffLat": f"{to_lat:.6f}", "dropoffLon": f"{to_lon:.6f}", "dropoffName": to_name or "",
        "pickupJson": json.dumps(pickup, separators=(",", ":"), ensure_ascii=False),
        "dropoffJson": json.dumps(drop, separators=(",", ":"), ensure_ascii=False),
    }
    for k, v in p.credentials.items():
        values[k] = v
    missing = [name for name in PLACEHOLDER.findall(h.template)
               if name in CREDENTIAL_PLACEHOLDERS and not values.get(name)]
    if missing:
        return {"url": fallback, "fallback": fallback, "kind": "url", "missingCredentials": missing}
    url = PLACEHOLDER.sub(lambda m: quote(str(values.get(m.group(1), "")), safe=""), h.template)
    return {"url": url, "fallback": fallback, "kind": "template"}


def handoff_api_url(base_url: str, city_id: str, provider_id: str, *, from_lat: float, from_lon: float,
                    to_lat: float, to_lon: float, from_name: str | None, to_name: str | None) -> str:
    """The API's own hand-off endpoint for this trip (clients add `platform` and follow the `url` it returns)."""
    q = {"providerId": provider_id, "fromLat": f"{from_lat:.6f}", "fromLon": f"{from_lon:.6f}",
         "toLat": f"{to_lat:.6f}", "toLon": f"{to_lon:.6f}"}
    if from_name:
        q["fromName"] = from_name[:120]
    if to_name:
        q["toName"] = to_name[:120]
    return f"{base_url.rstrip('/')}/v1/cities/{city_id}/ondemand/handoff?{urlencode(q)}"


# ------------------------------------------------------------------ car routing (OTP direct CAR)
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class CarRouter:
    """Direct CAR routes from OTP with a short cache (rounded coordinates, 5-minute time bucket)."""
    TTL_S = 30

    def __init__(self, otp):
        self.otp = otp
        self._cache: dict[tuple, tuple[float, dict]] = {}

    @staticmethod
    def _key(from_lat, from_lon, to_lat, to_lon, when: dt.datetime) -> tuple:
        return (round(from_lat, 4), round(from_lon, 4), round(to_lat, 4), round(to_lon, 4),
                int(when.timestamp() // 300))

    async def route(self, from_lat: float, from_lon: float, to_lat: float, to_lon: float,
                    when: dt.datetime) -> dict | None:
        key = self._key(from_lat, from_lon, to_lat, to_lon, when)
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < self.TTL_S:
            return hit[1]
        variables = {
            "origin": {"location": {"coordinate": {"latitude": from_lat, "longitude": from_lon}}},
            "destination": {"location": {"coordinate": {"latitude": to_lat, "longitude": to_lon}}},
            "dateTime": {"earliestDeparture": when.isoformat()},
            "modes": {"direct": ["CAR"], "directOnly": True}, "first": 1,
        }
        data = await self.otp.graphql(CAR_QUERY, variables)
        route = route_from_otp(data)
        if route:
            self._cache[key] = (now, route)
            if len(self._cache) > 500:
                oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:250]
                for k, _ in oldest:
                    self._cache.pop(k, None)
        return route


    async def probe(self, city: City) -> bool | None:
        """Does the graph route cars at all? One short route near the centre, cached for 10 minutes."""
        now = time.monotonic()
        cached = self._cache.get(("probe",))
        if cached and now - cached[0] < 600:
            return cached[1]["ok"]
        try:
            c = city.center
            r = await self.route(c.lat, c.lon, c.lat + 0.01, c.lon + 0.01, dt.datetime.now(dt.UTC))
            ok = r is not None
        except Exception:  # noqa: BLE001
            ok = False
        self._cache[("probe",)] = (now, {"ok": ok})
        return ok


def route_from_otp(data: dict) -> dict | None:
    conn = (data or {}).get("planConnection") or {}
    for edge in conn.get("edges") or []:
        node = edge.get("node") or {}
        legs = [lg for lg in (node.get("legs") or []) if lg and lg.get("mode") == "CAR"]
        if not legs:
            continue
        dist = sum(float(lg.get("distance") or 0) for lg in legs)
        dur = sum(float(lg.get("duration") or 0) for lg in legs)
        geom = (legs[0].get("legGeometry") or {}).get("points") if len(legs) == 1 else None
        return {"distanceMeters": round(dist), "durationSeconds": int(dur),
                "geometry": {"encoded": geom, "precision": 5} if geom else None}
    return None


# ------------------------------------------------------------------ estimates
def quote_provider(city: City, p: OnDemandProvider, *, distance_m: float, duration_s: float,
                   when: dt.datetime, points: list[tuple[float, float]], optional_ids: set[str] | None,
                   locale: str) -> dict:
    """Price (or None) for one provider on one ride."""
    price = None
    source = "none"
    if p.estimate.kind == "tariff":
        t = city.taxi_tariff(p.estimate.tariff_id)
        if t:
            price = tariff_estimate(t, distance_m, duration_s, when, country=city.country, points=points,
                                    optional_ids=optional_ids, locale=locale)
            source = "tariff"
    return {"providerId": p.id, "name": p.name, "kind": p.kind, "color": p.color, "textColor": p.text_color,
            "logoUrl": p.logo_url, "price": price, "waitSeconds": None, "source": source,
            "priceLabel": None if price else _lb(locale)["in_app"]}


def quotes_for(city: City, *, distance_m: float, duration_s: float, when: dt.datetime,
               from_lat: float, from_lon: float, to_lat: float, to_lon: float,
               from_name: str | None, to_name: str | None, base_url: str, provider_ids: set[str] | None = None,
               optional_ids: set[str] | None = None, locale: str = "es") -> list[dict]:
    points = [(from_lat, from_lon), (to_lat, to_lon)]
    out = []
    for p in city.on_demand_providers():
        if provider_ids and p.id not in provider_ids:
            continue
        q = quote_provider(city, p, distance_m=distance_m, duration_s=duration_s, when=when, points=points,
                           optional_ids=optional_ids, locale=locale)
        q["handoffUrl"] = handoff_api_url(base_url, city.id, p.id, from_lat=from_lat, from_lon=from_lon,
                                          to_lat=to_lat, to_lon=to_lon, from_name=from_name, to_name=to_name)
        out.append(q)
    return out


def recommended(quotes: list[dict]) -> str | None:
    """Cheapest priced provider, else the first configured one."""
    priced = [q for q in quotes if q.get("price")]
    if priced:
        return min(priced, key=lambda q: q["price"]["amount"])["providerId"]
    return quotes[0]["providerId"] if quotes else None


# ------------------------------------------------------------------ plan decoration
def is_ondemand_leg(leg: dict) -> bool:
    return leg.get("mode") == "CAR" and not leg.get("transit") and not leg.get("rental")


def attach_to_plan(city: City, plan: dict, *, when: dt.datetime, base_url: str, locale: str = "es") -> dict:
    """Add the `onDemand` block to every CAR leg, mark `modesUsed`, and fold the recommended price into the
    itinerary fare (kind "ondemand"; unknown prices leave a note instead of a number)."""
    from .features import estimate_fare
    for it in plan.get("itineraries", []):
        touched = False
        legs = it.get("legs", [])
        for idx, leg in enumerate(legs):
            if not is_ondemand_leg(leg):
                continue
            f, t = leg.get("from") or {}, leg.get("to") or {}
            start = _parse(leg.get("startTime")) or when
            _stretch_car_leg(it, idx, duration_factor(city, start))
            quotes = quotes_for(city, distance_m=float(leg.get("distanceMeters") or 0),
                                duration_s=float(leg.get("durationSeconds") or 0), when=start,
                                from_lat=f.get("lat"), from_lon=f.get("lon"), to_lat=t.get("lat"),
                                to_lon=t.get("lon"), from_name=f.get("name"), to_name=t.get("name"),
                                base_url=base_url, locale=locale)
            rec = recommended(quotes)
            kinds = {q["kind"] for q in quotes}
            kind = "taxi" if kinds == {"taxi"} else ("ridehail" if kinds == {"ridehail"} else "mixed")
            leg["onDemand"] = {"kind": kind, "providers": quotes, "recommendedProviderId": rec}
            touched = True
        if touched:
            it["modesUsed"] = [("CAR_ONDEMAND" if m == "CAR" else m) for m in it.get("modesUsed", [])]
            it["fare"] = estimate_fare(city, it["legs"], locale)
    return plan


def _shift(s: str | None, seconds: int) -> str | None:
    d = _parse(s)
    return (d + dt.timedelta(seconds=seconds)).isoformat() if d else s


def _stretch_car_leg(it: dict, idx: int, factor: float) -> None:
    """Apply the traffic factor to one CAR leg of an itinerary, keeping the timeline consistent: a car ride that
    feeds a transit leg starts earlier (you must leave sooner to catch the same bus); any other ride ends later
    and pushes the legs after it. The itinerary duration grows by the same delta."""
    legs = it.get("legs", [])
    leg = legs[idx]
    base = int(leg.get("durationSeconds") or 0)
    delta = int(round(base * factor)) - base
    if delta <= 0:
        return
    leg["durationSeconds"] = base + delta
    leg["durationFactor"] = factor
    feeds_transit = any(lg.get("transit") for lg in legs[idx + 1:])
    if feeds_transit:
        leg["startTime"] = _shift(leg.get("startTime"), -delta)
        for lg in legs[:idx]:
            lg["startTime"], lg["endTime"] = _shift(lg.get("startTime"), -delta), _shift(lg.get("endTime"), -delta)
        it["startTime"] = _shift(it.get("startTime"), -delta)
    else:
        leg["endTime"] = _shift(leg.get("endTime"), delta)
        for lg in legs[idx + 1:]:
            lg["startTime"], lg["endTime"] = _shift(lg.get("startTime"), delta), _shift(lg.get("endTime"), delta)
        it["endTime"] = _shift(it.get("endTime"), delta)
    it["durationSeconds"] = int(it.get("durationSeconds") or 0) + delta


def _parse(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
