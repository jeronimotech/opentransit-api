"""
GBFS (General Bikeshare Feed Specification) client, v2.x/v3.0 tolerant.

One `GbfsNetwork` per configured network. The API polls the feed itself and serves N clients from memory:
discovery (gbfs.json) and the slow-changing feeds (system_information, vehicle_types, system_pricing_plans,
station_information) are cached for a long time; station_status honours the feed's `ttl` (typically 30 s).
Nothing is proxied per request.

GBFS 3.0 differences handled here: localized strings are `[{text, language}]`, availability is
`num_vehicles_available` (2.x: `num_bikes_available`), `last_reported` is RFC 3339 (2.x: unix seconds).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .cities import BikeShareNetwork

log = logging.getLogger("ot.gbfs")

Fetcher = Callable[[str], Awaitable[dict]]
_ELECTRIC = {"electric_assist", "electric", "combustion_electric", "hybrid", "plug_in_hybrid"}
# Operators keep years of campaign plans in system_pricing_plans (Bogotá lists 140+): tests, free promos,
# subsidised (Sisbén), partner (bff/colaboradores) and "special bike" variants. The estimate must be what a
# casual rider pays for one trip today, so: exclude those, prefer single-ride plans, take the modal price.
_EXCLUDE_PLAN = re.compile(r"(test|teste|prueba|demo|gratis|gr[aá]tis|free|promo|sisb[eé]n|bff|colaborador|"
                           r"especial|especiale|aumento|desbloqueio|black friday|off\b|contra reloj|semana de|"
                           r"sin carro|power)", re.I)
_SINGLE_PLAN = re.compile(r"(\b1\s*viaje|per ride|single|sencillo|avulso|un viaje|one ride)", re.I)
_DAY_PLAN = re.compile(r"(diario|\bday\b|\bd[ií]a\b|pase|pass)", re.I)
_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")
_LONG_TTL = 3600
_INFO_TTL = 600


def text(value: Any, lang: str = "es") -> str | None:
    """GBFS 3.0 localized string (`[{text, language}]`) or a plain string -> one string (es, en, first)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        by_lang = {(v.get("language") or "").split("-")[0].lower(): v.get("text") for v in value
                   if isinstance(v, dict)}
        for cand in (lang, "es", "en"):
            if by_lang.get(cand):
                return str(by_lang[cand]).strip()
        for v in value:
            if isinstance(v, dict) and v.get("text"):
                return str(v["text"]).strip()
    return None


def iso_from(value: Any) -> str | None:
    """RFC 3339 string or unix seconds -> ISO-8601 UTC."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return dt.datetime.fromtimestamp(value, dt.UTC).isoformat().replace("+00:00", "Z")
    s = str(value)
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return s


def clean_plan_name(name: str | None) -> str | None:
    return _TAG.sub("", name).strip() if name else None


def pick_price_plan(plans: list[dict]) -> dict | None:
    """The plan a casual rider pays for one trip: single-ride plans first (else day passes, else anything),
    excluding tests/promos/subsidised/partner variants; among candidates the most common price wins
    (operators republish the same plan per campaign), ties go to the highest (latest tariff)."""
    real = [p for p in plans if p.get("price") is not None and float(p["price"]) > 0
            and not _EXCLUDE_PLAN.search(p.get("name") or "")]
    if not real:
        return None
    pool = [p for p in real if _SINGLE_PLAN.search(p.get("name") or "")] \
        or [p for p in real if _DAY_PLAN.search(p.get("name") or "")] or real
    counts: dict[float, int] = {}
    for p in pool:
        counts[float(p["price"])] = counts.get(float(p["price"]), 0) + 1
    best_price = max(counts, key=lambda v: (counts[v], v))
    return next(p for p in pool if float(p["price"]) == best_price)


def _money(v: float) -> str:
    return f"${int(v):,}".replace(",", ".") if float(v).is_integer() else f"${v}"


class GbfsNetwork:
    def __init__(self, city_id: str, cfg: BikeShareNetwork, fetcher: Fetcher | None = None,
                 client: httpx.AsyncClient | None = None):
        self.city_id = city_id
        self.cfg = cfg
        self._fetch = fetcher or self._http_fetch
        self._cli = client or httpx.AsyncClient(timeout=15, follow_redirects=True,
                                                headers={"User-Agent": "opentransit-api (+gbfs)"})
        self._lock = asyncio.Lock()
        self.feeds: dict[str, str] = {}
        self.version: str | None = None
        self.ttl: int = 30
        self._fetched: dict[str, float] = {}            # feed name -> monotonic time of last fetch
        self.system: dict = {}
        self.vehicle_types: dict[str, dict] = {}
        self.pricing_plans: list[dict] = []
        self.station_info: dict[str, dict] = {}
        self.station_status: dict[str, dict] = {}
        self.last_status_at: float = 0.0
        self.last_error: str | None = None
        self.http_status: int | None = None

    # ------------------------------------------------------------------ fetching
    async def _http_fetch(self, url: str) -> dict:
        r = await self._cli.get(url)
        self.http_status = r.status_code
        r.raise_for_status()
        return r.json()

    async def close(self) -> None:
        await self._cli.aclose()

    def _stale(self, name: str, ttl: int) -> bool:
        return time.monotonic() - self._fetched.get(name, -1e9) > ttl

    async def _discover(self, force: bool = False) -> None:
        if self.feeds and not force and not self._stale("gbfs", _LONG_TTL):
            return
        doc = await self._fetch(self.cfg.gbfs_url)
        self.version = str(doc.get("version") or "")
        self.ttl = max(10, int(doc.get("ttl") or 30))
        data = doc.get("data") or {}
        feeds = data.get("feeds")
        if feeds is None:                       # GBFS 2.x: data.<lang>.feeds
            lang = next(iter(data), None)
            feeds = (data.get(lang) or {}).get("feeds") if lang else []
        self.feeds = {f["name"]: f["url"] for f in (feeds or []) if f.get("name") and f.get("url")}
        self._fetched["gbfs"] = time.monotonic()

    async def _load(self, name: str, ttl: int, force: bool = False) -> dict | None:
        if not force and not self._stale(name, ttl):
            return None
        url = self.feeds.get(name)
        if not url:
            return None
        doc = await self._fetch(url)
        self._fetched[name] = time.monotonic()
        return doc.get("data") or {}

    async def refresh(self, force: bool = False) -> None:
        """Refresh whatever is stale (cheap when nothing is). Safe to call on every request."""
        async with self._lock:
            try:
                await self._discover(force)
                if d := await self._load("system_information", _LONG_TTL, force):
                    self.system = d
                if d := await self._load("vehicle_types", _LONG_TTL, force):
                    self.vehicle_types = {v["vehicle_type_id"]: v for v in d.get("vehicle_types") or []
                                          if v.get("vehicle_type_id")}
                if d := await self._load("system_pricing_plans", _LONG_TTL, force):
                    self.pricing_plans = [self._plan(p) for p in d.get("plans") or []]
                if d := await self._load("station_information", _INFO_TTL, force):
                    self.station_info = {s["station_id"]: s for s in d.get("stations") or [] if s.get("station_id")}
                if d := await self._load("station_status", self.ttl, force):
                    self.station_status = {s["station_id"]: s for s in d.get("stations") or []
                                           if s.get("station_id")}
                    self.last_status_at = time.time()
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("[%s/%s] GBFS refresh failed: %s", self.city_id, self.cfg.id, self.last_error)

    async def poll_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.refresh()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.ttl)
            except TimeoutError:
                pass

    # ------------------------------------------------------------------ views
    def _plan(self, p: dict) -> dict:
        return {"id": p.get("plan_id"), "name": text(p.get("name")), "price": p.get("price"),
                "currency": p.get("currency"), "description": text(p.get("description")) or None,
                "isTaxable": bool(p.get("is_taxable"))}

    def vehicle_type(self, vid: str | None) -> dict | None:
        v = self.vehicle_types.get(vid or "")
        if not v:
            return None
        return {"id": v["vehicle_type_id"], "formFactor": v.get("form_factor"),
                "propulsion": v.get("propulsion_type"), "name": text(v.get("name")) or v["vehicle_type_id"]}

    def is_electric(self, vid: str | None) -> bool:
        v = self.vehicle_types.get(vid or "") or {}
        return (v.get("propulsion_type") or "") in _ELECTRIC

    def public_id(self, station_id: str) -> str:
        return f"{self.cfg.id}:{station_id}"

    def station(self, station_id: str, *, detail: bool = False) -> dict | None:
        """Merged information + status for one station, in the public shape. `station_id` may be raw or scoped."""
        raw = station_id.split(":", 1)[1] if station_id.startswith(self.cfg.id + ":") else station_id
        info = self.station_info.get(raw)
        if not info:
            return None
        st = self.station_status.get(raw) or {}
        avail = st.get("num_vehicles_available", st.get("num_bikes_available"))
        types = st.get("vehicle_types_available") or []
        ebikes = sum(int(t.get("count") or 0) for t in types if self.is_electric(t.get("vehicle_type_id")))
        out = {
            "id": self.public_id(raw), "networkId": self.cfg.id, "kind": "rental_station",
            "name": text(info.get("name")) or raw, "lat": info.get("lat"), "lon": info.get("lon"),
            "capacity": info.get("capacity"),
            "vehiclesAvailable": int(avail) if avail is not None else None,
            "ebikesAvailable": ebikes if types else None,
            "docksAvailable": st.get("num_docks_available"),
            "isInstalled": bool(st.get("is_installed", True)), "isRenting": bool(st.get("is_renting", True)),
            "isReturning": bool(st.get("is_returning", True)),
            "lastReported": iso_from(st.get("last_reported")),
        }
        if detail:
            out["vehicleTypesAvailable"] = [
                {**(self.vehicle_type(t.get("vehicle_type_id")) or {"id": t.get("vehicle_type_id"),
                                                                   "formFactor": None, "propulsion": None,
                                                                   "name": t.get("vehicle_type_id")}),
                 "count": int(t.get("count") or 0)} for t in types]
            out["network"] = self.summary()
        return out

    def stations(self, bbox: tuple[float, float, float, float] | None = None, limit: int = 500) -> list[dict]:
        out = []
        for sid, info in self.station_info.items():
            lat, lon = info.get("lat"), info.get("lon")
            if lat is None or lon is None:
                continue
            if bbox and not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
                continue
            s = self.station(sid)
            if s:
                out.append(s)
            if len(out) >= limit:
                break
        return out

    def nearest(self, lat: float, lon: float, radius_m: float, limit: int) -> list[dict]:
        from .geo import haversine_m
        found = []
        for sid, info in self.station_info.items():
            if info.get("lat") is None:
                continue
            d = haversine_m(lat, lon, info["lat"], info["lon"])
            if d <= radius_m:
                s = self.station(sid)
                if s:
                    found.append({**s, "distanceMeters": int(round(d))})
        found.sort(key=lambda s: s["distanceMeters"])
        return found[:limit]

    def price_estimate(self) -> dict | None:
        """Configured `single_trip_price` wins; otherwise the GBFS pricing heuristic."""
        cfg = self.cfg.single_trip_price
        if cfg and cfg.get("amount") is not None:
            return {"amount": float(cfg["amount"]), "currency": cfg.get("currency") or "COP",
                    "label": cfg.get("label") or "1 viaje", "estimated": True}
        plan = pick_price_plan(self.pricing_plans)
        if not plan:
            return None
        return {"amount": plan["price"], "currency": plan.get("currency") or "COP",
                "label": clean_plan_name(plan.get("name")) or "Pase", "estimated": True}

    def pricing_summary(self) -> str | None:
        """Configured text, else "1 viaje $4.850 · Diario $11.000 · Mensual $34.650" from real plans."""
        if self.cfg.pricing_summary:
            return self.cfg.pricing_summary
        parts: list[str] = []
        single = self.price_estimate()
        if single:
            parts.append(f"{single['label']} {_money(single['amount'])}")
        real = [p for p in self.pricing_plans if p.get("price") and float(p["price"]) > 0
                and not _EXCLUDE_PLAN.search(p.get("name") or "")]
        for word in ("diario", "mensual", "anual"):
            cands = [p for p in real if word in (clean_plan_name(p.get("name")) or "").lower()]
            if cands:
                # most common price, latest tariff on ties (same rule as the single-trip estimate)
                counts: dict[float, int] = {}
                for p in cands:
                    counts[float(p["price"])] = counts.get(float(p["price"]), 0) + 1
                price = max(counts, key=lambda v: (counts[v], v))
                parts.append(f"{word.capitalize()} {_money(price)}")
        return " · ".join(parts) or None

    def vehicles_available(self) -> int:
        return sum(int(s.get("num_vehicles_available", s.get("num_bikes_available")) or 0)
                   for s in self.station_status.values())

    def age_seconds(self) -> int | None:
        return int(time.time() - self.last_status_at) if self.last_status_at else None

    def up(self) -> bool:
        age = self.age_seconds()
        return bool(self.station_info) and age is not None and age <= max(120, 4 * self.ttl) \
            and self.last_error is None

    def summary(self) -> dict:
        return {
            **self.cfg.public(), "pricingSummary": self.pricing_summary(),
            "systemId": self.system.get("system_id"), "systemName": text(self.system.get("name")),
            "timezone": self.system.get("timezone"), "gbfsVersion": self.version,
            "stations": len(self.station_info), "vehiclesAvailable": self.vehicles_available(),
            "vehicleTypes": [self.vehicle_type(v) for v in self.vehicle_types],
            "pricingPlans": self.pricing_plans,
            "lastFetchAt": iso_from(self.last_status_at) if self.last_status_at else None,
            "up": self.up(), "error": self.last_error,
        }

    def health(self) -> dict:
        return {"id": self.cfg.id, "up": self.up(), "stations": len(self.station_info),
                "vehiclesAvailable": self.vehicles_available(), "ageSeconds": self.age_seconds(),
                "error": self.last_error}
