"""
Admin-editable city configuration.

The city YAML is the base. Admins may override a few sections (fares, client config, links, services,
primary colour, mobility networks and the white-label landing page) through
`/v1/admin/cities/{city}/config`; the override is persisted, deep-merged on top
of the YAML, validated strictly, and swapped into the city runtime so `/v1/cities/{city}` and fare
estimation reflect it immediately. Everything else in the YAML (feeds, OTP, agencies...) is not editable.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .cities import (
    AppConfig,
    BikeShareNetwork,
    City,
    Fares,
    Landing,
    Links,
    Maintenance,
    MinAppVersion,
    Mobility,
    OnDemandEstimateCfg,
    OnDemandHandoff,
    OnDemandPolicy,
    OnDemandProvider,
    ServiceTile,
    TaxiSurcharge,
    TaxiSurchargeWhen,
    TaxiTariff,
    TaxiZone,
)
from .db import pool
from .errors import ApiError
from .ondemand import PLACEHOLDER, mask_credentials

log = logging.getLogger("ot.admin_config")

EDITABLE = ("fares", "config", "links", "services", "branding", "mobility", "landing")
SERVICE_ICONS = ("card", "report", "help", "link", "bike", "parking", "taxi", "ticket", "info", "map")
LANDING_ICONS = ("route", "live", "board", "bike", "open", "alert", "accessibility", "favorites", "offline", "map",
                 "ticket", "info")


# ------------------------------------------------------------------ strict validation (camelCase, public shape)
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _https(v: str | None) -> str | None:
    if v is None:
        return None
    if not v.startswith("https://"):
        raise ValueError("must be an https:// URL")
    return v


class FaresCfg(_Strict):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    base: float = Field(ge=0)
    transfer: float = Field(0, ge=0)
    transferWindowMinutes: int = Field(110, ge=0, le=600)
    maxTransfers: int = Field(2, ge=0, le=5)
    note: str | None = Field(None, max_length=300)
    estimated: bool = True


class MinAppVersionCfg(_Strict):
    ios: str = Field("1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    android: str = Field("1.0.0", pattern=r"^\d+\.\d+\.\d+$")


class MaintenanceCfg(_Strict):
    active: bool = False
    message: str | None = Field(None, max_length=500)


class ConfigCfg(_Strict):
    vehiclePollSeconds: int = Field(15, ge=5, le=120)
    departuresRefreshSeconds: int = Field(20, ge=5, le=120)
    features: dict[str, bool] = {}
    minAppVersion: MinAppVersionCfg = MinAppVersionCfg()
    maintenance: MaintenanceCfg = MaintenanceCfg()


class LinksCfg(_Strict):
    pqrs: str | None = None
    recharge: str | None = None
    support: str | None = None
    privacy: str | None = None

    _v = field_validator("pqrs", "recharge", "support", "privacy")(_https)


class ServiceCfg(_Strict):
    id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    label: str = Field(min_length=1, max_length=60)
    icon: Literal[SERVICE_ICONS] = "link"  # type: ignore[valid-type]
    url: str
    kind: Literal["external", "internal", "deeplink"] = "external"

    _v = field_validator("url")(_https)


class BrandingCfg(_Strict):
    primaryColor: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class AppLinksCfg(_Strict):
    ios: str | None = None
    android: str | None = None

    _v = field_validator("ios", "android")(_https)


class SingleTripPriceCfg(_Strict):
    amount: float = Field(ge=0)
    currency: str = Field("COP", pattern=r"^[A-Z]{3}$")
    label: str | None = Field(None, max_length=60)


class BikeShareCfg(_Strict):
    id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    name: str = Field(min_length=1, max_length=80)
    network: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,60}$")
    gbfsUrl: str
    color: str = Field("#00A859", pattern=r"^#[0-9A-Fa-f]{6}$")
    url: str | None = None
    apps: AppLinksCfg = AppLinksCfg()
    pricingSummary: str | None = Field(None, max_length=160)
    singleTripPrice: SingleTripPriceCfg | None = None
    formFactors: list[Literal["bicycle", "scooter", "cargo_bicycle", "moped", "car", "other"]] = ["bicycle"]

    _v = field_validator("gbfsUrl", "url")(_https)


# ---- v1.4 on-demand mobility (taxi tariffs, providers, policy)
_HM = r"^([01]\d|2[0-3]):[0-5]\d$"


class TaxiSurchargeWhenCfg(_Strict):
    nightFrom: str | None = Field(None, pattern=_HM)
    nightTo: str | None = Field(None, pattern=_HM)
    sundays: bool = False
    holidays: bool = False
    zones: list[str] = []
    optional: bool = False


class TaxiSurchargeCfg(_Strict):
    id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    label: str = Field(min_length=1, max_length=60)
    amount: float = Field(ge=0)
    when: TaxiSurchargeWhenCfg = TaxiSurchargeWhenCfg()


class TaxiZoneCfg(_Strict):
    id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    name: str = Field(min_length=1, max_length=80)
    polygon: list[list[float]] = Field(min_length=3, max_length=500)


class TaxiTariffCfg(_Strict):
    id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    name: str = Field(min_length=1, max_length=80)
    currency: str = Field("COP", pattern=r"^[A-Z]{3}$")
    flagFall: float = Field(ge=0)
    unitPrice: float = Field(ge=0)
    unitMeters: int = Field(100, gt=0, le=5000)
    unitSeconds: int = Field(30, gt=0, le=600)
    minimumFare: float = Field(0, ge=0)
    surcharges: list[TaxiSurchargeCfg] = Field([], max_length=12)
    zones: list[TaxiZoneCfg] = Field([], max_length=20)
    source: dict[str, str | None] | None = None
    validFrom: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    note: str | None = Field(None, max_length=200)
    waitingShare: float = Field(0.15, ge=0, le=0.6)
    rounding: int = Field(100, gt=0)
    bandPct: float = Field(0.10, ge=0, le=0.5)


class OnDemandEstimateCfgA(_Strict):
    kind: Literal["tariff", "api", "none"] = "none"
    tariffId: str | None = None


class OnDemandHandoffCfg(_Strict):
    kind: Literal["none", "url", "template"] = "url"
    template: str | None = Field(None, max_length=1000)
    web: str | None = None
    apps: AppLinksCfg = AppLinksCfg()
    scheme: str | None = Field(None, pattern=r"^[a-z][a-z0-9+.-]*://")

    _v = field_validator("web")(_https)

    @field_validator("template")
    @classmethod
    def _tpl(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("https://"):
            raise ValueError("must be an https:// URL template")
        return v


class OnDemandProviderCfg(_Strict):
    id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["taxi", "ridehail"] = "ridehail"
    color: str = Field("#333333", pattern=r"^#[0-9A-Fa-f]{6}$")
    textColor: str = Field("#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    logoUrl: str | None = None
    estimate: OnDemandEstimateCfgA = OnDemandEstimateCfgA()
    handoff: OnDemandHandoffCfg = OnDemandHandoffCfg()
    credentials: dict[str, str] = {}
    enabled: bool = True
    order: int = Field(0, ge=0, le=100)

    _v = field_validator("logoUrl")(_https)


class OnDemandPolicyCfg(_Strict):
    maxDirectDistanceKm: float = Field(40, gt=0, le=300)
    firstLastMile: bool = True
    maxFeederKm: float = Field(8, gt=0, le=50)
    showWhenTransitFaster: bool = True


class MobilityCfg(_Strict):
    bikeShare: list[BikeShareCfg] = []
    taxiTariffs: list[TaxiTariffCfg] = Field([], max_length=10)
    onDemand: list[OnDemandProviderCfg] = Field([], max_length=20)
    onDemandPolicy: OnDemandPolicyCfg = OnDemandPolicyCfg()


# ---- landing (v1.3): every URL https (or null); CTA urls may also be "#anchor" / "/path"; sizes bounded
def _https_or_local(v: str | None) -> str | None:
    if v is None or v.startswith("#") or v.startswith("/"):
        return v
    return _https(v)


class LandingCtaCfg(_Strict):
    label: str = Field(min_length=1, max_length=40)
    url: str | None = None

    _v = field_validator("url")(_https_or_local)


class LandingHeroCfg(_Strict):
    title: str = Field("", max_length=80)
    subtitle: str = Field("", max_length=200)
    ctaPrimary: LandingCtaCfg = LandingCtaCfg(label="Abrir la app")
    ctaSecondary: LandingCtaCfg | None = None


class LandingThemeCfg(_Strict):
    primaryColor: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    accentColor: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    logoUrl: str | None = None
    heroImageUrl: str | None = None
    darkHero: bool = True

    _v = field_validator("logoUrl", "heroImageUrl")(_https)


class LandingAppsCfg(_Strict):
    ios: str | None = None
    android: str | None = None
    web: str | None = None

    _v = field_validator("ios", "android", "web")(_https)


class LandingHighlightCfg(_Strict):
    icon: Literal[LANDING_ICONS] = "info"  # type: ignore[valid-type]
    title: str = Field(min_length=1, max_length=60)
    text: str = Field("", max_length=160)


class LandingScreenshotCfg(_Strict):
    url: str
    alt: str = Field("", max_length=120)
    kind: Literal["mobile", "web"] = "mobile"

    _v = field_validator("url")(_https)


class LandingStatsCfg(_Strict):
    show: bool = True
    items: list[Literal["routes", "stops", "vehiclesLive", "bikeStations", "alertsActive"]] = \
        ["routes", "stops", "vehiclesLive", "bikeStations", "alertsActive"]


class LandingPartnerCfg(_Strict):
    name: str = Field(min_length=1, max_length=80)
    logoUrl: str | None = None
    url: str | None = None
    role: str | None = Field(None, max_length=80)

    _v = field_validator("logoUrl", "url")(_https)


class LandingLinkCfg(_Strict):
    label: str = Field(min_length=1, max_length=60)
    url: str

    _v = field_validator("url")(_https)


class LandingOpenDataCfg(_Strict):
    show: bool = True
    links: list[LandingLinkCfg] = Field([], max_length=12)


class LandingFaqCfg(_Strict):
    q: str = Field(min_length=1, max_length=160)
    a: str = Field(min_length=1, max_length=600)


class LandingSocialCfg(_Strict):
    x: str | None = None
    instagram: str | None = None
    facebook: str | None = None
    youtube: str | None = None
    github: str | None = None

    _v = field_validator("x", "instagram", "facebook", "youtube", "github")(_https)


class LandingContactCfg(_Strict):
    email: str | None = Field(None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    url: str | None = None
    social: LandingSocialCfg = LandingSocialCfg()

    _v = field_validator("url")(_https)


class LandingFooterCfg(_Strict):
    legalName: str | None = Field(None, max_length=120)
    privacyUrl: str | None = None
    termsUrl: str | None = None
    attribution: str | None = Field(None, max_length=300)

    _v = field_validator("privacyUrl", "termsUrl")(_https)


class LandingSeoCfg(_Strict):
    title: str | None = Field(None, max_length=70)
    description: str | None = Field(None, max_length=160)
    ogImageUrl: str | None = None

    _v = field_validator("ogImageUrl")(_https)


class LandingCfg(_Strict):
    enabled: bool = False
    slug: str | None = Field(None, pattern=r"^[a-z0-9-]{1,40}$")
    locale: str | None = Field(None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    theme: LandingThemeCfg = LandingThemeCfg()
    hero: LandingHeroCfg = LandingHeroCfg()
    apps: LandingAppsCfg = LandingAppsCfg()
    highlights: list[LandingHighlightCfg] = Field([], max_length=8)
    screenshots: list[LandingScreenshotCfg] = Field([], max_length=8)
    stats: LandingStatsCfg = LandingStatsCfg()
    partners: list[LandingPartnerCfg] = Field([], max_length=12)
    openData: LandingOpenDataCfg = LandingOpenDataCfg()
    faq: list[LandingFaqCfg] = Field([], max_length=12)
    contact: LandingContactCfg = LandingContactCfg()
    footer: LandingFooterCfg = LandingFooterCfg()
    seo: LandingSeoCfg = LandingSeoCfg()


class ConfigPatch(BaseModel):
    """PUT body: any subset of the editable sections (JSON null removes that section's override)."""
    model_config = ConfigDict(extra="forbid")
    fares: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    links: dict[str, Any] | None = None
    services: list[dict[str, Any]] | None = None
    branding: dict[str, Any] | None = None
    mobility: dict[str, Any] | None = None
    landing: dict[str, Any] | None = None
    note: str | None = Field(None, max_length=300)
    updatedBy: str | None = Field(None, max_length=120)


# ------------------------------------------------------------------ merge + apply
def deep_merge(base: dict, patch: dict) -> dict:
    """Recursive merge; dict values merge, everything else (lists, scalars) replaces; None deletes the key."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def yaml_sections(base: City) -> dict:
    """The editable sections of the YAML city in the public camelCase shape."""
    pub = base.public()
    return {"fares": pub["fares"], "config": pub["config"], "links": pub["links"], "services": pub["services"],
            "branding": {"primaryColor": pub["branding"]["primaryColor"]},
            "mobility": base.mobility_public(admin=True),      # credentials included (masked by describe())
            "landing": base.landing.public()}


def _validate(section: str, model: type[BaseModel], value: Any) -> dict:
    try:
        return model.model_validate(value).model_dump()
    except ValidationError as e:
        first = e.errors()[0]
        path = ".".join(str(x) for x in first.get("loc", []))
        where = f"{section}.{path}" if path else section
        raise ApiError(f"{where}: {first.get('msg')}", status=422) from None


def validate_sections(sections: dict) -> dict:
    """Strict validation of the *effective* sections; raises ApiError(422) with a field path."""
    out: dict = {"fares": _validate("fares", FaresCfg, sections["fares"]) if sections.get("fares") else None,
                 "config": _validate("config", ConfigCfg, sections.get("config") or {}),
                 "links": _validate("links", LinksCfg, sections.get("links") or {}),
                 "services": [_validate(f"services.{i}", ServiceCfg, s)
                              for i, s in enumerate(sections.get("services") or [])],
                 "branding": _validate("branding", BrandingCfg, sections.get("branding") or {}),
                 "mobility": _validate("mobility", MobilityCfg, sections.get("mobility") or {}),
                 "landing": _validate("landing", LandingCfg, sections.get("landing") or {})}
    ids = [s["id"] for s in out["services"]]
    if len(ids) != len(set(ids)):
        raise ApiError("services: duplicate service id", status=422)
    nids = [n["id"] for n in out["mobility"]["bikeShare"]]
    if len(nids) != len(set(nids)):
        raise ApiError("mobility.bikeShare: duplicate network id", status=422)
    _validate_ondemand(out["mobility"])
    return out


def _validate_ondemand(mob: dict) -> None:
    tids = [t["id"] for t in mob["taxiTariffs"]]
    if len(tids) != len(set(tids)):
        raise ApiError("mobility.taxiTariffs: duplicate tariff id", status=422)
    for i, t in enumerate(mob["taxiTariffs"]):
        zone_ids = {z["id"] for z in t["zones"]}
        for j, sc in enumerate(t["surcharges"]):
            unknown = [z for z in sc["when"]["zones"] if z not in zone_ids]
            if unknown:
                raise ApiError(f"mobility.taxiTariffs.{i}.surcharges.{j}.when.zones: unknown zone {unknown[0]}",
                               status=422)
    pids = [p["id"] for p in mob["onDemand"]]
    if len(pids) != len(set(pids)):
        raise ApiError("mobility.onDemand: duplicate provider id", status=422)
    orders = [p["order"] for p in mob["onDemand"]]
    if len(orders) != len(set(orders)):
        raise ApiError("mobility.onDemand: order must be unique", status=422)
    for i, p in enumerate(mob["onDemand"]):
        est, h = p["estimate"], p["handoff"]
        if est["kind"] == "tariff" and est["tariffId"] not in tids:
            raise ApiError(f"mobility.onDemand.{i}.estimate.tariffId: unknown tariff", status=422)
        if h["kind"] == "template":
            if not h["template"] or not PLACEHOLDER.search(h["template"]):
                raise ApiError(f"mobility.onDemand.{i}.handoff.template: must contain at least one {{placeholder}}",
                               status=422)


def build_city(base: City, sections: dict) -> City:
    """Effective City = YAML city with the validated editable sections applied."""
    upd: dict = {}
    f = sections.get("fares")
    upd["fares"] = Fares(currency=f["currency"], base=f["base"], transfer=f["transfer"],
                         transfer_window_minutes=f["transferWindowMinutes"], max_transfers=f["maxTransfers"],
                         note=f.get("note")) if f else None
    c = sections["config"]
    upd["config"] = AppConfig(vehicle_poll_seconds=c["vehiclePollSeconds"],
                              departures_refresh_seconds=c["departuresRefreshSeconds"], features=c["features"],
                              min_app_version=MinAppVersion(**c["minAppVersion"]),
                              maintenance=Maintenance(**c["maintenance"]))
    upd["links"] = Links(**sections["links"])
    upd["services"] = [ServiceTile(**s) for s in sections["services"]]
    upd["branding"] = base.branding.model_copy(update={"primary_color": sections["branding"]["primaryColor"]})
    nets = [BikeShareNetwork(id=n["id"], name=n["name"], network=n["network"], gbfs_url=n["gbfsUrl"],
                             color=n["color"], url=n.get("url"), apps=n.get("apps") or {},
                             pricing_summary=n.get("pricingSummary"), single_trip_price=n.get("singleTripPrice"),
                             form_factors=n.get("formFactors") or [])
            for n in sections["mobility"]["bikeShare"]]
    mob = sections["mobility"]
    tariffs = [TaxiTariff(id=t["id"], name=t["name"], currency=t["currency"], flag_fall=t["flagFall"],
                          unit_price=t["unitPrice"], unit_meters=t["unitMeters"], unit_seconds=t["unitSeconds"],
                          minimum_fare=t["minimumFare"],
                          surcharges=[TaxiSurcharge(id=x["id"], label=x["label"], amount=x["amount"],
                                                    when=TaxiSurchargeWhen(night_from=x["when"]["nightFrom"],
                                                                           night_to=x["when"]["nightTo"],
                                                                           sundays=x["when"]["sundays"],
                                                                           holidays=x["when"]["holidays"],
                                                                           zones=x["when"]["zones"],
                                                                           optional=x["when"]["optional"]))
                                      for x in t["surcharges"]],
                          zones=[TaxiZone(id=z["id"], name=z["name"], polygon=z["polygon"]) for z in t["zones"]],
                          source=t.get("source"), valid_from=t.get("validFrom"), note=t.get("note"),
                          waiting_share=t["waitingShare"], rounding=t["rounding"], band_pct=t["bandPct"])
               for t in mob["taxiTariffs"]]
    providers = [OnDemandProvider(id=p["id"], name=p["name"], kind=p["kind"], color=p["color"],
                                  text_color=p["textColor"], logo_url=p.get("logoUrl"),
                                  estimate=OnDemandEstimateCfg(kind=p["estimate"]["kind"],
                                                               tariff_id=p["estimate"].get("tariffId")),
                                  handoff=OnDemandHandoff(kind=p["handoff"]["kind"],
                                                          template=p["handoff"].get("template"),
                                                          web=p["handoff"].get("web"),
                                                          apps=p["handoff"].get("apps") or {},
                                                          scheme=p["handoff"].get("scheme")),
                                  credentials={k: v for k, v in (p.get("credentials") or {}).items() if v},
                                  enabled=p["enabled"], order=p["order"])
                 for p in mob["onDemand"]]
    pol = mob["onDemandPolicy"]
    upd["mobility"] = Mobility(bike_share=nets, taxi_tariffs=tariffs, on_demand=providers,
                               on_demand_policy=OnDemandPolicy(max_direct_distance_km=pol["maxDirectDistanceKm"],
                                                               first_last_mile=pol["firstLastMile"],
                                                               max_feeder_km=pol["maxFeederKm"],
                                                               show_when_transit_faster=pol["showWhenTransitFaster"]))
    upd["features"] = base.features.model_copy(update={"bike_share": bool(nets),
                                                       "on_demand": any(p.enabled for p in providers)})
    upd["landing"] = Landing.model_validate(sections["landing"])
    return base.model_copy(update=upd)


def effective_city(base: City, override: dict | None) -> City:
    merged = deep_merge(yaml_sections(base), override or {})
    return build_city(base, validate_sections(merged))


# ------------------------------------------------------------------ storage
class ConfigStore(Protocol):
    async def load(self, city_id: str) -> dict | None: ...
    async def save(self, city_id: str, data: dict, updated_by: str | None, note: str | None) -> dict: ...
    async def clear(self, city_id: str, changed_by: str | None) -> dict: ...
    async def history(self, city_id: str, limit: int) -> list[dict]: ...


def _iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") if t else None


class PgConfigStore:
    async def load(self, city_id: str) -> dict | None:
        async with pool().acquire() as c:
            r = await c.fetchrow("SELECT data, revision, updated_at, updated_by FROM city_config_override "
                                 "WHERE city_id=$1", city_id)
        if not r:
            return None
        data = r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
        return {"data": data, "revision": r["revision"], "updatedAt": _iso(r["updated_at"]),
                "updatedBy": r["updated_by"]}

    async def save(self, city_id: str, data: dict, updated_by: str | None, note: str | None) -> dict:
        async with pool().acquire() as c, c.transaction():
            rev = await c.fetchval(
                """INSERT INTO city_config_override (city_id, data, revision, updated_at, updated_by)
                   VALUES ($1, $2::jsonb, 1, now(), $3)
                   ON CONFLICT (city_id) DO UPDATE SET data=EXCLUDED.data,
                       revision=city_config_override.revision + 1, updated_at=now(), updated_by=EXCLUDED.updated_by
                   RETURNING revision""", city_id, json.dumps(data), updated_by)
            await c.execute("INSERT INTO city_config_history (city_id, revision, data, changed_by, note) "
                            "VALUES ($1,$2,$3::jsonb,$4,$5)", city_id, rev, json.dumps(data), updated_by, note)
        return await self.load(city_id)  # type: ignore[return-value]

    async def clear(self, city_id: str, changed_by: str | None) -> dict:
        async with pool().acquire() as c, c.transaction():
            prev = await c.fetchval("SELECT revision FROM city_config_override WHERE city_id=$1", city_id) or 0
            await c.execute("DELETE FROM city_config_override WHERE city_id=$1", city_id)
            await c.execute("INSERT INTO city_config_history (city_id, revision, data, changed_by, note) "
                            "VALUES ($1,$2,'{}'::jsonb,$3,'reset')", city_id, prev + 1, changed_by)
        return {"data": None, "revision": prev + 1, "updatedAt": None, "updatedBy": changed_by}

    async def history(self, city_id: str, limit: int) -> list[dict]:
        async with pool().acquire() as c:
            rows = await c.fetch("SELECT revision, data, changed_at, changed_by, note FROM city_config_history "
                                 "WHERE city_id=$1 ORDER BY revision DESC, id DESC LIMIT $2", city_id, limit)
        return [{"revision": r["revision"], "changedAt": _iso(r["changed_at"]), "changedBy": r["changed_by"],
                 "note": r["note"], "data": r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])}
                for r in rows]


class MemoryConfigStore:
    """In-memory store (tests, and dev without a database)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.log: dict[str, list[dict]] = {}

    async def load(self, city_id: str) -> dict | None:
        return copy.deepcopy(self.rows.get(city_id))

    async def save(self, city_id: str, data: dict, updated_by: str | None, note: str | None) -> dict:
        rev = (self.rows.get(city_id) or {"revision": 0})["revision"] + 1
        now = _iso(dt.datetime.now(dt.UTC))
        self.rows[city_id] = {"data": copy.deepcopy(data), "revision": rev, "updatedAt": now, "updatedBy": updated_by}
        self.log.setdefault(city_id, []).insert(0, {"revision": rev, "changedAt": now, "changedBy": updated_by,
                                                    "note": note, "data": copy.deepcopy(data)})
        return copy.deepcopy(self.rows[city_id])

    async def clear(self, city_id: str, changed_by: str | None) -> dict:
        rev = (self.rows.pop(city_id, None) or {"revision": 0})["revision"] + 1
        self.log.setdefault(city_id, []).insert(0, {"revision": rev, "changedAt": _iso(dt.datetime.now(dt.UTC)),
                                                    "changedBy": changed_by, "note": "reset", "data": {}})
        return {"data": None, "revision": rev, "updatedAt": None, "updatedBy": changed_by}

    async def history(self, city_id: str, limit: int) -> list[dict]:
        return copy.deepcopy(self.log.get(city_id, [])[:limit])


# ------------------------------------------------------------------ runtime glue
def apply_to_runtime(rt, row: dict | None) -> None:
    """Swap the effective City into the runtime (invalidates every cached view of the config)."""
    if rt.base_city is None:
        rt.base_city = rt.city
    override = (row or {}).get("data") or None
    rt.city = effective_city(rt.base_city, override)
    rt.override = override
    rt.config_revision = (row or {}).get("revision", 0) or 0
    rt.config_updated_at = (row or {}).get("updatedAt")
    rt.config_updated_by = (row or {}).get("updatedBy")


async def load_overrides(store: ConfigStore, runtimes: dict) -> None:
    for rt in runtimes.values():
        try:
            apply_to_runtime(rt, await store.load(rt.city.id))
            if rt.override:
                log.info("[%s] config override r%s applied (%s)", rt.city.id, rt.config_revision,
                         ", ".join(sorted(rt.override)))
        except Exception:  # noqa: BLE001
            log.exception("[%s] could not apply the stored config override; serving the YAML", rt.city.id)


def describe(rt) -> dict:
    """Shape shared by GET/PUT/DELETE of the admin config endpoint."""
    base = rt.base_city or rt.city
    return {"effective": rt.city.public(), "override": mask_credentials(rt.override),
            "yaml": mask_credentials(yaml_sections(base)),
            "revision": rt.config_revision, "updatedAt": rt.config_updated_at, "updatedBy": rt.config_updated_by,
            "editable": list(EDITABLE)}
