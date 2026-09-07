"""The ten tools the model may call. Each one runs our own code path in-process — never HTTP to ourselves.

The point of this file is that the model has no way to state a transit fact except by calling one of these,
so a hallucinated departure time is structurally impossible: it can only repeat what a tool returned.

Every tool returns `(json_for_the_model, card_or_None)`. The card is what the client renders and taps
through to the real screen; the JSON is the compact version the model paraphrases in two or three lines.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request

from ..errors import ApiError
from ..runtime import CityRuntime

log = logging.getLogger("ot.assistant.tools")

MAX_RESULT_CHARS = 6000     # a tool result the model cannot finish reading is worse than a smaller one


@dataclass(slots=True)
class ToolContext:
    """Everything a tool may read. `request` is the live one: `plan` needs it to build deep links."""
    rt: CityRuntime
    request: Request
    locale: str = "es"
    user: dict | None = None            # {lat, lon, favorites} — never persisted, never logged


def _obj(props: dict, required: list[str]) -> dict:
    """Strict JSON schema: every provider we support rejects or mis-fills loose schemas."""
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_STR = {"type": "string"}
_NUM = {"type": "number"}


# ── declarations ──────────────────────────────────────────────────────────────
TOOLS: list[dict] = [
    {"name": "find_place",
     "description": "Resolve a place name, address, stop or station into coordinates. Call this before any "
                    "tool that needs a lat/lon you were not given. Never invent coordinates.",
     "schema": _obj({"query": _STR,
                     "near": {"type": ["string", "null"],
                              "description": "optional 'lat,lon' to bias the search toward"}},
                    ["query", "near"])},
    {"name": "plan_trip",
     "description": "Plan a door-to-door trip. Coordinates must come from the user's context or find_place.",
     "schema": _obj({"fromLat": _NUM, "fromLon": _NUM, "toLat": _NUM, "toLon": _NUM,
                     "time": {"type": ["string", "null"], "description": "ISO-8601; null means now"},
                     "arriveBy": {"type": ["boolean", "null"]},
                     "modes": {"type": ["array", "null"], "items": _STR,
                               "description": "e.g. TRANSIT, WALK, BICYCLE, BIKE_RENTAL"}},
                    ["fromLat", "fromLon", "toLat", "toLon", "time", "arriveBy", "modes"])},
    {"name": "next_departures",
     "description": "Next departures at a stop or station, grouped by route. Pass stopId when you have one, "
                    "otherwise stopQuery and it will be resolved first.",
     "schema": _obj({"stopId": {"type": ["string", "null"]}, "stopQuery": {"type": ["string", "null"]},
                     "routeId": {"type": ["string", "null"], "description": "optional filter"}},
                    ["stopId", "stopQuery", "routeId"])},
    {"name": "locate_bus",
     "description": "Where the next buses of one route are right now relative to a stop: live when the feed "
                    "has them, scheduled otherwise.",
     "schema": _obj({"stopId": _STR, "routeId": _STR}, ["stopId", "routeId"])},
    {"name": "service_alerts",
     "description": "Active service alerts, optionally for one route.",
     "schema": _obj({"routeId": {"type": ["string", "null"]}}, ["routeId"])},
    {"name": "fare_estimate",
     "description": "Estimated cost of a trip, including taxi and ride-hailing when the city has them.",
     "schema": _obj({"fromLat": _NUM, "fromLon": _NUM, "toLat": _NUM, "toLon": _NUM},
                    ["fromLat", "fromLon", "toLat", "toLon"])},
    {"name": "nearby_stops",
     "description": "Stops and stations near a point.",
     "schema": _obj({"lat": _NUM, "lon": _NUM, "radius": {"type": ["number", "null"]}},
                    ["lat", "lon", "radius"])},
    {"name": "bike_stations",
     "description": "Shared-bike stations near a point, with how many bikes and docks are free.",
     "schema": _obj({"lat": _NUM, "lon": _NUM, "radius": {"type": ["number", "null"]}},
                    ["lat", "lon", "radius"])},
    {"name": "vehicles_near",
     "description": "Buses currently moving near a point.",
     "schema": _obj({"lat": _NUM, "lon": _NUM, "radius": {"type": ["number", "null"]}},
                    ["lat", "lon", "radius"])},
    {"name": "route_info",
     "description": "Look up routes by name or number: their component, long name and today's service hours.",
     "schema": _obj({"routeQuery": _STR}, ["routeQuery"])},
]

TOOL_NAMES = {t["name"] for t in TOOLS}


# ── helpers ───────────────────────────────────────────────────────────────────
def _num(v: Any, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _body(res: Any) -> dict:
    """Routers return either a plain dict or a JSONResponse; the assistant only wants the dict."""
    return res if isinstance(res, dict) else json.loads(res.body)


async def _candidates(ctx: ToolContext, query: str, near: str | None = None) -> list[dict]:
    """The geocoder's ranked hits. Asking for several rather than one matters twice over: the ranker needs
    a pool to rank, and a query like «Parque de la 93» can match a station whose name merely starts the
    same way, so the model is shown the runners-up and can ask which one the user meant."""
    from ..geocode import geocode
    lat = lon = None
    if near and "," in str(near):
        a, b = str(near).split(",", 1)
        lat, lon = _num(a), _num(b)
    return await geocode(ctx.rt.city, str(query)[:120], lat, lon, 5)


async def _resolve(ctx: ToolContext, query: str, near: str | None = None) -> dict | None:
    """The single door through which a place becomes coordinates."""
    hits = await _candidates(ctx, query, near)
    return hits[0] if hits else None


def _trim(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "…"


# ── dispatch ──────────────────────────────────────────────────────────────────
async def run_tool(ctx: ToolContext, name: str, args: dict) -> tuple[str, dict | None]:
    """Execute one tool. Returns (json for the model, card for the client).

    Failures become readable tool results rather than exceptions: the model can then say what went wrong
    or ask for the missing detail, where a raised error would abandon a turn the user is watching.
    """
    try:
        data, card = await _dispatch(ctx, name, args)
    except ApiError as exc:
        return json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False), None
    except Exception:
        log.exception("assistant tool %s failed", name)
        return json.dumps({"error": "TOOL_FAILED", "message": f"{name} could not be completed"}), None
    return _trim(data), card


async def _dispatch(ctx: ToolContext, name: str, args: dict) -> tuple[Any, dict | None]:
    rt, city = ctx.rt, ctx.rt.city

    if name == "find_place":
        hits = await _candidates(ctx, args.get("query") or "", args.get("near"))
        if not hits:
            return {"found": False, "query": args.get("query")}, None
        hit = hits[0]
        others = [{"name": h.get("name"), "label": h.get("label"), "lat": h.get("lat"), "lon": h.get("lon"),
                   "stopId": h.get("stopId"), "type": h.get("type")} for h in hits[1:4]]
        return ({"found": True, "name": hit.get("name"), "label": hit.get("label"),
                 "lat": hit.get("lat"), "lon": hit.get("lon"),
                 "stopId": hit.get("stopId"), "type": hit.get("type"),
                 # if the best hit does not look like what was asked for, say so instead of planning from it
                 "alternatives": others},
                {"kind": "place", "payload": hit})

    if name in ("plan_trip", "fare_estimate"):
        from ..routers.plan import plan
        on_demand = name == "fare_estimate"
        body = _body(await plan(
            request=ctx.request, rt=rt,
            fromLat=_num(args.get("fromLat")), fromLon=_num(args.get("fromLon")),
            toLat=_num(args.get("toLat")), toLon=_num(args.get("toLon")),
            time=args.get("time"), arriveBy=bool(args.get("arriveBy") or False),
            modes=",".join(args["modes"]) if args.get("modes") else None,
            onDemand=on_demand, wheelchair=False,
            numItineraries=3, maxWalkDistance=1500, locale=ctx.locale,
            fromName=None, toName=None))
        its = body.get("itineraries", [])[:3]
        slim = [{"departAt": i.get("startTime"), "arriveAt": i.get("endTime"),
                 "minutes": round((i.get("durationSeconds") or 0) / 60),
                 "transfers": i.get("transfers"), "modes": i.get("modesUsed"),
                 "fare": i.get("fare"),
                 "routes": [(leg.get("route") or {}).get("shortName")
                            for leg in i.get("legs", []) if leg.get("transit")]}
                for i in its]
        if on_demand:
            return ({"options": slim, "currency": city.fares.currency if city.fares else None},
                    {"kind": "fares", "payload": {"itineraries": its}} if its else None)
        return ({"itineraries": slim, "warnings": body.get("warnings", [])},
                {"kind": "itineraries",
                 "payload": {"from": body.get("from"), "to": body.get("to"), "itineraries": its}}
                if its else None)

    if name == "next_departures":
        stop_id = args.get("stopId")
        if not stop_id and args.get("stopQuery"):
            hit = await _resolve(ctx, args["stopQuery"])
            stop_id = (hit or {}).get("stopId")
            if not stop_id:
                return {"error": "STOP_NOT_FOUND", "query": args.get("stopQuery")}, None
        if not stop_id:
            return {"error": "BAD_REQUEST", "message": "stopId or stopQuery is required"}, None
        from ..routers.board import board
        body = _body(await board(stopId=city.unscoped(str(stop_id)), rt=rt, minutes=60, perRoute=3))
        want = str(args.get("routeId") or "")
        rows = [r for r in body.get("rows", []) if r.get("next")]
        if want:
            unscoped = city.unscoped(want)
            rows = [r for r in rows
                    if city.unscoped((r.get("route") or {}).get("id") or "") == unscoped
                    or (r.get("route") or {}).get("shortName") == want]
        rows = rows[:8]
        slim = [{"route": (r.get("route") or {}).get("shortName"), "headsign": r.get("headsign"),
                 "next": [{"minutes": n.get("minutes"), "live": n.get("realtime")}
                          for n in r.get("next", [])]}
                for r in rows]
        return ({"stop": (body.get("stop") or {}).get("name"), "rows": slim,
                 "realtime": (body.get("freshness") or {}).get("realtime")},
                {"kind": "board", "payload": {**body, "rows": rows}})

    if name == "locate_bus":
        from ..routers.board import next_buses
        body = _body(await next_buses(stopId=city.unscoped(str(args.get("stopId") or "")),
                                      routeId=city.unscoped(str(args.get("routeId") or "")),
                                      rt=rt, limit=3, minutes=90))
        slim = [{"minutes": n.get("minutes"), "source": n.get("source"), "stopsAway": n.get("stopsAway"),
                 "metres": n.get("distanceMeters")} for n in body.get("next", [])]
        return ({"stop": (body.get("stop") or {}).get("name"),
                 "route": (body.get("route") or {}).get("shortName"),
                 "servesStop": body.get("servesStop"), "vehiclesOnRoute": body.get("vehiclesOnRoute"),
                 "next": slim},
                {"kind": "next", "payload": body})

    if name == "service_alerts":
        from ..routers.alerts import alerts
        body = _body(await alerts(rt=rt, routeId=args.get("routeId"), stopId=None, active=True))
        every = body.get("alerts", [])
        items = every[:5]
        slim = [{"header": a.get("header"), "effect": a.get("effect"), "severity": a.get("severity"),
                 "routes": [r.get("shortName") for r in (a.get("routes") or [])][:6]} for a in items]
        return ({"count": len(every), "alerts": slim},
                {"kind": "alerts", "payload": {"alerts": items}} if items else None)

    if name in ("nearby_stops", "bike_stations", "vehicles_near"):
        lat, lon = _num(args.get("lat")), _num(args.get("lon"))
        if lat is None or lon is None:
            return {"error": "BAD_REQUEST", "message": "lat and lon are required"}, None
        radius = int(min(max(_num(args.get("radius"), 500) or 500, 50), 3000))

        if name == "vehicles_near":
            from ..geo import haversine_m
            from ..routers.vehicles import vehicles
            body = _body(await vehicles(rt=rt, routeId=None, component=None, bbox=None))
            near = sorted(({**v, "metres": round(haversine_m(lat, lon, v["lat"], v["lon"]))}
                           for v in body.get("vehicles", []) if v.get("lat") is not None),
                          key=lambda v: v["metres"])
            near = [v for v in near if v["metres"] <= radius][:8]
            slim = [{"route": v.get("routeShortName"), "metres": v["metres"],
                     "component": v.get("component")} for v in near]
            return ({"vehicles": slim, "count": len(near)},
                    {"kind": "vehicles", "payload": {"vehicles": near}} if near else None)

        from ..routers.stops import nearby
        body = _body(await nearby(rt=rt, lat=lat, lon=lon, radius=radius, limit=6,
                                  include="rental" if name == "bike_stations" else "stops"))
        if name == "bike_stations":
            stations = body.get("rentalStations", [])[:5]
            slim = [{"name": s.get("name"), "bikes": s.get("vehiclesAvailable"),
                     "ebikes": s.get("ebikesAvailable"), "docks": s.get("docksAvailable"),
                     "metres": s.get("distanceMeters")} for s in stations]
            return ({"stations": slim},
                    {"kind": "bikeStations", "payload": {"stations": stations}} if stations else None)
        stops = body.get("stops", [])
        slim = [{"name": s.get("name"), "stopId": s.get("id"), "metres": s.get("distanceMeters"),
                 "component": s.get("component")} for s in stops]
        return {"stops": slim}, {"kind": "stops", "payload": {"stops": stops}} if stops else None

    if name == "route_info":
        from ..routers.routes import list_routes
        body = _body(await list_routes(rt=rt, component=None, q=str(args.get("routeQuery") or "")[:60]))
        found = body.get("routes", [])[:5]
        slim = [{"routeId": r.get("id"), "shortName": r.get("shortName"), "longName": r.get("longName"),
                 "component": r.get("component"), "serviceWindow": r.get("serviceWindow")} for r in found]
        return {"routes": slim}, {"kind": "routes", "payload": {"routes": found}} if found else None

    return {"error": "UNKNOWN_TOOL", "message": f"no tool named {name}"}, None


# ── system prompt ─────────────────────────────────────────────────────────────
def system_prompt(ctx: ToolContext) -> str:
    """The rule that matters is the first one: facts come from tools, never from the model's memory."""
    city = ctx.rt.city
    now = dt.datetime.now(ZoneInfo(city.timezone)).strftime("%Y-%m-%d %H:%M")
    english = (ctx.locale or city.locale or "es").startswith("en")
    lines = [
        f"Eres el asistente de transporte público de {city.name}. Ahora son las {now}, hora local.",
        "",
        "Regla principal: NUNCA inventes horarios, rutas, paradas, precios ni tiempos de llegada. Todo dato "
        "concreto tiene que venir de una herramienta que llamaste en este mismo turno. Si ninguna herramienta "
        "puede responder, dilo en una frase y ofrece lo más cercano que sí puedas hacer.",
        "",
        "Coordenadas: usa solo las del contexto del usuario o las que devuelva find_place. Si no tienes "
        "coordenadas de un lugar, llama primero a find_place. Cuando necesites varias cosas independientes, "
        "pide todas las herramientas a la vez.",
        "",
        "Estilo: dos o tres frases, sin markdown ni listas largas. La aplicación ya muestra los detalles en "
        "tarjetas debajo de tu respuesta, así que resume lo esencial en vez de repetir cada dato. Si un dato "
        "viene del horario y no del tiempo real, dilo.",
    ]
    user = ctx.user or {}
    if user.get("lat") is not None and user.get("lon") is not None:
        lines.append(f"\nUbicación actual del usuario: {user['lat']},{user['lon']} "
                     "(úsala cuando diga «aquí», «cerca» o «desde donde estoy»).")
    if user.get("favorites"):
        lines.append("Favoritos del usuario: " + json.dumps(user["favorites"], ensure_ascii=False)[:400])
    # The question's language wins over the app's. Somebody with an English phone
    # still asks "¿a qué hora pasa el bus?" and expects an answer in Spanish;
    # keying off the locale alone answered every Spanish question in English.
    app_lang = "English" if english else "Spanish"
    lines.append(
        f"\nIdioma: responde SIEMPRE en el mismo idioma en que el usuario escribió su última pregunta, "
        f"aunque el resto de la conversación esté en otro. Si la pregunta es demasiado corta o ambigua para "
        f"saberlo (por ejemplo, solo el nombre de un lugar), responde en {app_lang}, que es el idioma de la "
        f"aplicación."
    )
    extra = city.config.assistant.system_extra
    if extra:
        lines.append("\n" + extra)
    return "\n".join(lines)
