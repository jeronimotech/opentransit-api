"""City (tenant) registry. Loaded once from cities/*.yaml; `${VAR}` / `${VAR:-default}` are expanded."""
import logging
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

log = logging.getLogger("ot.cities")

Component = Literal["trunk", "feeder", "dual", "zonal", "cable", "rail", "other"]
# The default may itself be a reference: `${OTP_MYCITY_URL:-${OTP_URL:-http://localhost:8080}}`.
# Innermost references (whose default contains no `${`) are resolved first, until nothing is left.
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-((?:(?!\$\{)[^}])*))?\}")


def expand_env(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), text)
    return text


class LatLon(BaseModel):
    lat: float
    lon: float


class Branding(BaseModel):
    primary_color: str = "#1565C0"
    logo_url: str | None = None


class Features(BaseModel):
    realtime_vehicles: bool = False
    trip_updates: bool = False
    alerts: bool = False
    fares: bool = False
    bike_share: bool = False
    on_demand: bool = False
    open_mobility: bool = False


class Feeds(BaseModel):
    gtfs_static_url: str
    rt_positions_url: str | None = None
    rt_tripupdates_url: str | None = None
    rt_alerts_url: str | None = None
    poll_seconds: int = 20
    static_refresh_hours: int = 24


class Otp(BaseModel):
    base_url: str
    feed_id: str


class Geocoder(BaseModel):
    photon_url: str | None = "https://photon.komoot.io"


class AgencyCfg(BaseModel):
    id: str
    name: str
    component: Component = "other"
    color: str | None = None


class ComponentCfg(BaseModel):
    """How a component (trunk, feeder, ...) is labelled and drawn in the apps."""
    id: Component
    label: str
    color: str
    icon: Literal["brt", "bus", "cable", "rail", "tram", "ferry"] = "bus"


class Fares(BaseModel):
    """Flat-fare estimate used when the GTFS publishes no fares. All amounts in minor-free units (COP)."""
    currency: str = "COP"
    base: float
    transfer: float = 0
    transfer_window_minutes: int = 110
    max_transfers: int = 2
    note: str | None = None


class MinAppVersion(BaseModel):
    ios: str = "1.0.0"
    android: str = "1.0.0"


class Maintenance(BaseModel):
    active: bool = False
    message: str | None = None


class AnalyticsConfig(BaseModel):
    """First-party, privacy-preserving analytics (v1.5): can be switched off per city; k-anonymity on reads."""
    enabled: bool = True
    retention_days: int = 90
    k_threshold: int = 5


class ShareConfig(BaseModel):
    """Shareable ETA links (v1.7). Anonymous, short-lived, revocable; rows dropped at expiry."""
    enabled: bool = True
    ttl_minutes: int = 180
    max_ttl_minutes: int = 720


class ApnsConfig(BaseModel):
    """Credentials for Live Activity pushes. Empty by default: the app updates its own activity locally."""
    key_id: str | None = None
    team_id: str | None = None
    bundle_id: str | None = None
    key_path: str | None = None


class PushConfig(BaseModel):
    """Optional server-driven Live Activity updates (v1.7 A4). Disabled means the client drives them."""
    enabled: bool = False
    apns: ApnsConfig = ApnsConfig()


class AppConfig(BaseModel):
    """Remote-configurable client behaviour (Maas pattern): polling, feature flags, forced update."""
    vehicle_poll_seconds: int = 15
    departures_refresh_seconds: int = 20
    features: dict[str, bool] = {"liveVehicles": True, "board": True, "pois": True, "followAlong": True,
                                 "bike": True}
    min_app_version: MinAppVersion = MinAppVersion()
    maintenance: Maintenance = Maintenance()
    analytics: AnalyticsConfig = AnalyticsConfig()
    share: ShareConfig = ShareConfig()
    push: PushConfig = PushConfig()


class Links(BaseModel):
    pqrs: str | None = None
    recharge: str | None = None
    support: str | None = None
    privacy: str | None = None


class BikeShareNetwork(BaseModel):
    """One shared-vehicle network published as GBFS (v1.2). `network` is the OTP updater network id."""
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    network: str
    gbfs_url: str
    color: str = "#00A859"
    url: str | None = None
    apps: dict[str, str | None] = {}
    pricing_summary: str | None = None
    single_trip_price: dict | None = None     # {amount, currency, label}: overrides the GBFS pricing heuristic
    form_factors: list[str] = ["bicycle"]

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "network": self.network, "gbfsUrl": self.gbfs_url,
                "color": self.color, "url": self.url,
                "apps": {"ios": self.apps.get("ios"), "android": self.apps.get("android")},
                "pricingSummary": self.pricing_summary, "singleTripPrice": self.single_trip_price,
                "formFactors": self.form_factors}


# ── v1.4 · on-demand mobility (taxi / ride-hailing), provider-agnostic ─────────────
class TaxiSurchargeWhen(BaseModel):
    """When a surcharge applies: a nightly window (may wrap midnight), Sundays, public holidays (by the city's
    country), a tariff zone touched by the trip, or only on request (`optional`, e.g. door-to-door)."""
    night_from: str | None = None      # "HH:MM" local
    night_to: str | None = None
    sundays: bool = False
    holidays: bool = False
    zones: list[str] = []
    optional: bool = False


class TaxiSurcharge(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    label: str
    amount: float = Field(ge=0)
    when: TaxiSurchargeWhen = TaxiSurchargeWhen()


class TaxiZone(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    polygon: list[list[float]]         # [[lon, lat], ...] (closed or not)


class TaxiTariff(BaseModel):
    """A regulated taximeter tariff. The estimate is flag fall + distance/waiting units, never below the minimum,
    plus the surcharges that apply; a ±band models traffic. The meter always wins — the apps say so."""
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    currency: str = "COP"
    flag_fall: float = Field(ge=0)
    unit_price: float = Field(ge=0)
    unit_meters: int = Field(100, gt=0)
    unit_seconds: int = Field(30, gt=0)
    minimum_fare: float = Field(0, ge=0)
    surcharges: list[TaxiSurcharge] = []
    zones: list[TaxiZone] = []
    source: dict | None = None         # {label, url}
    valid_from: str | None = None
    note: str | None = None
    waiting_share: float = Field(0.15, ge=0, le=0.6)   # share of the drive spent stopped (meter advances by time)
    rounding: int = Field(100, gt=0)   # amounts are rounded to this unit (COP taxis round to 100)
    band_pct: float = Field(0.10, ge=0, le=0.5)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "currency": self.currency, "flagFall": self.flag_fall,
                "unitPrice": self.unit_price, "unitMeters": self.unit_meters, "unitSeconds": self.unit_seconds,
                "minimumFare": self.minimum_fare,
                "surcharges": [{"id": x.id, "label": x.label, "amount": x.amount,
                                "when": {"nightFrom": x.when.night_from, "nightTo": x.when.night_to,
                                         "sundays": x.when.sundays, "holidays": x.when.holidays,
                                         "zones": x.when.zones, "optional": x.when.optional}}
                               for x in self.surcharges],
                "zones": [{"id": z.id, "name": z.name, "polygon": z.polygon} for z in self.zones],
                "source": self.source, "validFrom": self.valid_from, "note": self.note,
                "waitingShare": self.waiting_share, "rounding": self.rounding, "bandPct": self.band_pct}


class OnDemandEstimateCfg(BaseModel):
    kind: Literal["tariff", "api", "none"] = "none"
    tariff_id: str | None = None


class OnDemandHandoff(BaseModel):
    """How the apps hand the rider over to the provider: a deep-link template with placeholders, a plain
    url/store link, or nothing (name only)."""
    kind: Literal["none", "url", "template"] = "url"
    template: str | None = None
    web: str | None = None
    apps: dict[str, str | None] = {}
    scheme: str | None = None


class OnDemandProvider(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    kind: Literal["taxi", "ridehail"] = "ridehail"
    color: str = "#333333"
    text_color: str = "#FFFFFF"
    logo_url: str | None = None
    estimate: OnDemandEstimateCfg = OnDemandEstimateCfg()
    handoff: OnDemandHandoff = OnDemandHandoff()
    credentials: dict[str, str] = {}   # secret-ish (client ids...): never in public responses, masked in admin
    enabled: bool = True
    order: int = 0

    def public(self) -> dict:
        h = self.handoff
        return {"id": self.id, "name": self.name, "kind": self.kind, "color": self.color,
                "textColor": self.text_color, "logoUrl": self.logo_url,
                "estimate": {"kind": self.estimate.kind, "tariffId": self.estimate.tariff_id},
                "handoff": {"kind": h.kind, "hasTemplate": bool(h.template), "web": h.web,
                            "apps": {"ios": h.apps.get("ios"), "android": h.apps.get("android")},
                            "scheme": h.scheme},
                "enabled": self.enabled, "order": self.order}

    def admin(self) -> dict:
        """Full shape for the admin config (credentials included; the router masks them before replying)."""
        out = self.public()
        out["handoff"]["template"] = self.handoff.template
        del out["handoff"]["hasTemplate"]
        out["credentials"] = dict(self.credentials)
        return out


class OnDemandPolicy(BaseModel):
    max_direct_distance_km: float = Field(40, gt=0)
    first_last_mile: bool = True
    max_feeder_km: float = Field(8, gt=0)
    show_when_transit_faster: bool = True
    # OTP routes cars at free-flow speeds; real city traffic is slower. Car durations (estimate + plan legs +
    # the tariff's waiting units) are multiplied by `duration_factor`, or by `night_duration_factor` when the
    # departure falls inside the tariff's night window.
    duration_factor: float = Field(1.4, ge=1.0, le=3.0)
    night_duration_factor: float = Field(1.1, ge=1.0, le=3.0)

    def public(self) -> dict:
        return {"maxDirectDistanceKm": self.max_direct_distance_km, "firstLastMile": self.first_last_mile,
                "maxFeederKm": self.max_feeder_km, "showWhenTransitFaster": self.show_when_transit_faster,
                "durationFactor": self.duration_factor, "nightDurationFactor": self.night_duration_factor}


class Mobility(BaseModel):
    """Shared / on-demand mobility attached to the city. Bike-share first; scooters ride the same GBFS."""
    bike_share: list[BikeShareNetwork] = []
    taxi_tariffs: list[TaxiTariff] = []
    on_demand: list[OnDemandProvider] = []
    on_demand_policy: OnDemandPolicy = OnDemandPolicy()


# ── v1.6 · Open Mobility Foundation: CDS 1.1.0 curbs + MDS 2.1.0 policy/geography ──
class CdsCurbsCfg(BaseModel):
    """Where the curb inventory comes from: our own admin-edited copy, or a third-party CDS Curbs feed."""
    source: Literal["local", "url"] = "local"
    url: str | None = None
    refresh_minutes: int = 60


class CdsEventsProvider(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    token_hash: str | None = None      # sha256 of the operator's bearer token (phase B); never the token


class CdsEventsCfg(BaseModel):
    accept: bool = False               # phase B
    providers: list[CdsEventsProvider] = []


class Cds(BaseModel):
    enabled: bool = False
    curbs: CdsCurbsCfg = CdsCurbsCfg()
    publish: bool = False              # serve the CDS Curbs API for operators
    events: CdsEventsCfg = CdsEventsCfg()
    # CDS 1.1.0 has no currency field: a `rate` is an integer in "the smallest denomination of the local
    # currency". Bogotá quotes whole COP, not centavos, so the city says which currency and how many minor
    # units one integer is worth. Only our normalised `priceLabel` uses this; the verbatim CDS endpoints
    # keep the integers exactly as the spec mandates.
    rate_currency: str | None = None   # None -> the city's fare currency
    rate_minor_units: int = 1          # COP -> 1, USD/EUR (cents) -> 100


class MdsAuth(BaseModel):
    """How we authenticate against an operator's Provider API (`oauth2`/`bearer`) **and** how we authenticate
    an operator pushing into our Agency API (`jwt`: the bearer's claims carry `provider_id`). Phase B uses
    both directions; the shape is fixed now so the config never has to be rewritten."""
    kind: Literal["none", "bearer", "oauth2", "jwt"] = "none"
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    jwks_url: str | None = None            # agency direction: where the operator's public keys live
    issuer: str | None = None
    audience: str | None = None
    provider_id_claim: str = "provider_id"


class MdsProvider(BaseModel):
    """An operator the city polls (phase B). Declared now so the admin UI and config are stable."""
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    mode: Literal["micromobility", "passenger_services", "delivery_robots", "car_share", "transit"] = \
        "micromobility"
    base_url: str
    auth: MdsAuth = MdsAuth()
    ingest: list[Literal["vehicles", "status_changes", "trips", "events"]] = ["vehicles"]
    poll_minutes: int = 60
    credentials: dict[str, str] = {}   # secret-ish: never public, masked in admin
    enabled: bool = True


class Mds(BaseModel):
    enabled: bool = False
    version: str = "2.1.0"
    publish_policy: bool = False       # serve MDS Policy + Geography
    authority_url: str | None = None   # a third-party MDS Policy/Geography document we mirror
    refresh_minutes: int = 60
    providers: list[MdsProvider] = []
    retention_days: int = 90


class ParkRide(BaseModel):
    """Park & Ride combos (phase B): drive to a parking curb zone near a station, then walk + transit."""
    enabled: bool = False
    max_drive_km: float = 25.0
    max_walk_meters: int = 600
    default_dwell_hours: float = 8.0       # dwell assumed when estimating the parking fee


class OpenMobility(BaseModel):
    cds: Cds = Cds()
    mds: Mds = Mds()
    park_ride: ParkRide = ParkRide()


# ── v1.3 · configurable city landing page (white-label) ─────────────────────────
class _Camel(BaseModel):
    """Landing blocks accept snake_case (YAML) or camelCase (admin API) and serialise as camelCase."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class LandingCta(_Camel):
    label: str
    url: str | None = None          # None -> the web app's /{city}; "#anchor" and "/path" allowed


class LandingHero(_Camel):
    title: str = ""
    subtitle: str = ""
    cta_primary: LandingCta = LandingCta(label="Abrir la app")
    cta_secondary: LandingCta | None = None


class LandingTheme(_Camel):
    primary_color: str | None = None   # None -> branding.primary_color
    accent_color: str | None = None
    logo_url: str | None = None        # None -> branding.logo_url
    hero_image_url: str | None = None
    dark_hero: bool = True


class LandingApps(_Camel):
    ios: str | None = None
    android: str | None = None
    web: str | None = None


class LandingHighlight(_Camel):
    icon: str = "info"
    title: str
    text: str


class LandingScreenshot(_Camel):
    url: str
    alt: str = ""
    kind: Literal["mobile", "web"] = "mobile"


class LandingStats(_Camel):
    show: bool = True
    items: list[str] = ["routes", "stops", "vehiclesLive", "bikeStations", "alertsActive"]


class LandingPartner(_Camel):
    name: str
    logo_url: str | None = None
    url: str | None = None
    role: str | None = None


class LandingLink(_Camel):
    label: str
    url: str


class LandingOpenData(_Camel):
    show: bool = True
    links: list[LandingLink] = []      # empty -> derived from the feeds and bike-share config


class LandingFaq(_Camel):
    q: str
    a: str


class LandingSocial(_Camel):
    x: str | None = None
    instagram: str | None = None
    facebook: str | None = None
    youtube: str | None = None
    github: str | None = None


class LandingContact(_Camel):
    email: str | None = None
    url: str | None = None
    social: LandingSocial = LandingSocial()


class LandingFooter(_Camel):
    legal_name: str | None = None
    privacy_url: str | None = None     # None -> links.privacy
    terms_url: str | None = None
    attribution: str | None = None     # None -> city.attribution


class LandingSeo(_Camel):
    title: str | None = None
    description: str | None = None
    og_image_url: str | None = None


class Landing(_Camel):
    """Everything the white-label landing page needs; admin-editable, served by /v1/cities/{city}/landing."""
    enabled: bool = False
    slug: str | None = None
    locale: str | None = None          # None -> city.locale
    theme: LandingTheme = LandingTheme()
    hero: LandingHero = LandingHero()
    apps: LandingApps = LandingApps()
    highlights: list[LandingHighlight] = []
    screenshots: list[LandingScreenshot] = []
    stats: LandingStats = LandingStats()
    partners: list[LandingPartner] = []
    open_data: LandingOpenData = LandingOpenData()
    faq: list[LandingFaq] = []
    contact: LandingContact = LandingContact()
    footer: LandingFooter = LandingFooter()
    seo: LandingSeo = LandingSeo()

    def public(self) -> dict:
        return self.model_dump(by_alias=True)


class ServiceTile(BaseModel):
    """Hand-off tile to a partner or agency service (recharge, PQRS...). Never a core feature."""
    id: str
    label: str
    icon: str = "link"
    url: str
    kind: Literal["external", "internal", "deeplink"] = "external"


class City(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    country: str
    timezone: str
    locale: str = "en"
    center: LatLon
    bbox: list[float]
    default_zoom: int = 12
    modes: list[str] = ["WALK", "BUS"]
    branding: Branding = Branding()
    features: Features = Features()
    attribution: str = ""
    feeds: Feeds
    otp: Otp
    geocoder: Geocoder = Geocoder()
    agencies: list[AgencyCfg] = []
    components: list[ComponentCfg] = []
    fares: Fares | None = None
    config: AppConfig = AppConfig()
    links: Links = Links()
    services: list[ServiceTile] = []
    mobility: Mobility = Mobility()
    open_mobility: OpenMobility = OpenMobility()
    landing: Landing = Landing()
    pois_file: str | None = None      # path relative to the cities dir; default cities/<id>/pois.geojson

    @field_validator("bbox")
    @classmethod
    def _bbox(cls, v: list[float]) -> list[float]:
        if len(v) != 4 or v[0] >= v[2] or v[1] >= v[3]:
            raise ValueError("bbox must be [minLon, minLat, maxLon, maxLat]")
        return v

    # --- helpers used all over the API ---
    def component_of_agency(self, agency_id: str | None) -> Component:
        for a in self.agencies:
            if a.id == agency_id:
                return a.component
        return "other"

    def color_of_agency(self, agency_id: str | None) -> str | None:
        for a in self.agencies:
            if a.id == agency_id:
                return a.color
        return None

    def scoped(self, raw_id: str | None) -> str | None:
        """GTFS id -> feed-scoped id exactly as OTP exposes it (`bogota:1234`)."""
        if raw_id is None:
            return None
        return raw_id if raw_id.startswith(self.otp.feed_id + ":") else f"{self.otp.feed_id}:{raw_id}"

    def unscoped(self, scoped_id: str) -> str:
        prefix = self.otp.feed_id + ":"
        return scoped_id[len(prefix):] if scoped_id.startswith(prefix) else scoped_id

    def bike_network(self, network_id: str | None):
        """Our network id (`<id>`) or OTP's updater id (`<network>`) -> BikeShareNetwork."""
        for n in self.mobility.bike_share:
            if network_id in (n.id, n.network):
                return n
        return None

    # ---- v1.4 on-demand helpers ----
    def on_demand_providers(self) -> list[OnDemandProvider]:
        return sorted((p for p in self.mobility.on_demand if p.enabled), key=lambda p: (p.order, p.id))

    def on_demand_provider(self, provider_id: str | None) -> OnDemandProvider | None:
        for p in self.mobility.on_demand:
            if p.id == provider_id:
                return p
        return None

    def taxi_tariff(self, tariff_id: str | None) -> TaxiTariff | None:
        for t in self.mobility.taxi_tariffs:
            if t.id == tariff_id:
                return t
        return None

    def on_demand_enabled(self) -> bool:
        return self.features.on_demand and bool(self.on_demand_providers())

    def mobility_public(self, *, admin: bool = False) -> dict:
        return {"bikeShare": [n.public() for n in self.mobility.bike_share],
                "taxiTariffs": [t.public() for t in self.mobility.taxi_tariffs],
                "onDemand": [(p.admin() if admin else p.public()) for p in self.mobility.on_demand],
                "onDemandPolicy": self.mobility.on_demand_policy.public()}

    # ---- v1.6 open mobility (CDS curbs / MDS policy) helpers ----
    def open_mobility_enabled(self) -> bool:
        om = self.open_mobility
        return self.features.open_mobility and (om.cds.enabled or om.mds.enabled)

    def open_mobility_public(self, *, admin: bool = False) -> dict:
        om = self.open_mobility
        cds = {"enabled": om.cds.enabled,
               "curbs": {"source": om.cds.curbs.source, "url": om.cds.curbs.url,
                         "refreshMinutes": om.cds.curbs.refresh_minutes},
               "publish": om.cds.publish,
               "rateCurrency": self.rate_currency(),
               "rateMinorUnits": om.cds.rate_minor_units,
               "events": {"accept": om.cds.events.accept,
                          "providers": [{"id": p.id, "name": p.name} for p in om.cds.events.providers]}}
        mds = {"enabled": om.mds.enabled, "version": om.mds.version, "publishPolicy": om.mds.publish_policy,
               "authorityUrl": om.mds.authority_url, "refreshMinutes": om.mds.refresh_minutes,
               "retentionDays": om.mds.retention_days,
               "providers": [self._mds_provider_public(p, admin=admin) for p in om.mds.providers]}
        pr = om.park_ride
        return {"cds": cds, "mds": mds,
                "parkRide": {"enabled": pr.enabled, "maxDriveKm": pr.max_drive_km,
                             "maxWalkMeters": pr.max_walk_meters, "defaultDwellHours": pr.default_dwell_hours}}

    def rate_currency(self) -> str:
        """Currency the CDS `rate` integers are quoted in (CDS itself has no currency field)."""
        return self.open_mobility.cds.rate_currency or (self.fares.currency if self.fares else "USD")

    @staticmethod
    def _mds_provider_public(p: "MdsProvider", *, admin: bool) -> dict:
        out = {"id": p.id, "name": p.name, "mode": p.mode, "baseUrl": p.base_url,
               "auth": {"kind": p.auth.kind, "tokenUrl": p.auth.token_url, "jwksUrl": p.auth.jwks_url,
                        "issuer": p.auth.issuer, "audience": p.auth.audience,
                        "providerIdClaim": p.auth.provider_id_claim},
               "ingest": list(p.ingest), "pollMinutes": p.poll_minutes, "enabled": p.enabled}
        if admin:
            out["credentials"] = dict(p.credentials)
        return out

    def transit_modes(self) -> list[str]:
        return [m for m in self.modes if m not in ("WALK", "BICYCLE", "CAR", "SCOOTER")]

    def public(self) -> dict:
        """Shape returned by /v1/cities (camelCase, no feed URLs or secrets)."""
        return {
            "id": self.id, "name": self.name, "country": self.country, "timezone": self.timezone,
            "locale": self.locale, "center": self.center.model_dump(), "bbox": self.bbox,
            "defaultZoom": self.default_zoom, "modes": self.modes,
            "branding": {"primaryColor": self.branding.primary_color, "logoUrl": self.branding.logo_url},
            "features": {
                "realtimeVehicles": self.features.realtime_vehicles, "tripUpdates": self.features.trip_updates,
                "alerts": self.features.alerts, "fares": self.features.fares, "bikeShare": self.features.bike_share,
                "onDemand": self.on_demand_enabled(),
                "openMobility": self.open_mobility_enabled(),
            },
            "agencies": [{"id": a.id, "name": a.name, "component": a.component, "color": a.color}
                         for a in self.agencies],
            "components": [c.model_dump() for c in self.components] or self._components_from_agencies(),
            "fares": {"currency": self.fares.currency, "base": self.fares.base, "transfer": self.fares.transfer,
                      "transferWindowMinutes": self.fares.transfer_window_minutes,
                      "maxTransfers": self.fares.max_transfers, "note": self.fares.note, "estimated": True}
            if self.fares else None,
            "config": {"vehiclePollSeconds": self.config.vehicle_poll_seconds,
                       "departuresRefreshSeconds": self.config.departures_refresh_seconds,
                       "features": self.config.features,
                       "minAppVersion": self.config.min_app_version.model_dump(),
                       "maintenance": self.config.maintenance.model_dump(),
                       "analytics": {"enabled": self.config.analytics.enabled,
                                     "retentionDays": self.config.analytics.retention_days,
                                     "kThreshold": self.config.analytics.k_threshold},
                       "share": {"enabled": self.config.share.enabled,
                                 "ttlMinutes": self.config.share.ttl_minutes},
                       # credentials never leave the server; clients only need to know who drives the activity
                       "push": {"enabled": self.config.push.enabled}},
            "links": self.links.model_dump(),
            "services": [s.model_dump() for s in self.services],
            "mobility": self.mobility_public(),
            "openMobility": self.open_mobility_public(),
            "attribution": self.attribution,
        }

    def landing_public(self) -> dict:
        """Landing config with the documented fallbacks resolved (theme <- branding, open data <- feeds...)."""
        ld = self.landing.public()
        th = ld["theme"]
        th["primaryColor"] = th["primaryColor"] or self.branding.primary_color
        th["logoUrl"] = th["logoUrl"] or self.branding.logo_url
        ld["locale"] = ld["locale"] or self.locale
        if not ld["openData"]["links"]:
            f = self.feeds
            links = [{"label": "GTFS", "url": f.gtfs_static_url}]
            for label, url in (("GTFS-RT · posiciones", f.rt_positions_url),
                               ("GTFS-RT · llegadas", f.rt_tripupdates_url),
                               ("GTFS-RT · alertas", f.rt_alerts_url)):
                if url:
                    links.append({"label": label, "url": url})
            links += [{"label": f"GBFS · {n.name}", "url": n.gbfs_url} for n in self.mobility.bike_share]
            ld["openData"]["links"] = links
        ft = ld["footer"]
        ft["attribution"] = ft["attribution"] or self.attribution
        ft["privacyUrl"] = ft["privacyUrl"] or self.links.privacy
        return ld

    def _components_from_agencies(self) -> list[dict]:
        """Derived component palette when the YAML does not declare `components`."""
        labels = {"trunk": "Troncal", "feeder": "Alimentador", "dual": "Dual", "zonal": "Zonal", "cable": "Cable",
                  "rail": "Tren", "other": "Otro"}
        icons = {"trunk": "brt", "cable": "cable", "rail": "rail"}
        seen: dict[str, dict] = {}
        for a in self.agencies:
            seen.setdefault(a.component, {"id": a.component, "label": labels.get(a.component, a.component),
                                          "color": a.color or self.branding.primary_color,
                                          "icon": icons.get(a.component, "bus")})
        return list(seen.values())

    def component_style(self, component: str | None) -> dict | None:
        for c in self.components or [ComponentCfg(**x) for x in self._components_from_agencies()]:
            if c.id == component:
                return c.model_dump()
        return None


def load_city_file(path: Path) -> City:
    raw = yaml.safe_load(expand_env(path.read_text(encoding="utf-8")))
    city = City.model_validate(raw)
    if city.id != path.stem:
        raise ValueError(f"{path.name}: id '{city.id}' must equal the file name")
    return city


def load_registry(cities_dir: Path) -> dict[str, City]:
    reg: dict[str, City] = {}
    for p in sorted(cities_dir.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        city = load_city_file(p)
        reg[city.id] = city
        log.info("loaded city %s (%s)", city.id, city.name)
    if not reg:
        log.warning("no cities found in %s", cities_dir)
    return reg
