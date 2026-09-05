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


class AppConfig(BaseModel):
    """Remote-configurable client behaviour (Maas pattern): polling, feature flags, forced update."""
    vehicle_poll_seconds: int = 15
    departures_refresh_seconds: int = 20
    features: dict[str, bool] = {"liveVehicles": True, "board": True, "pois": True, "followAlong": True,
                                 "bike": True}
    min_app_version: MinAppVersion = MinAppVersion()
    maintenance: Maintenance = Maintenance()


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


class Mobility(BaseModel):
    """Shared / on-demand mobility attached to the city. Bike-share first; scooters ride the same GBFS."""
    bike_share: list[BikeShareNetwork] = []


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
                       "maintenance": self.config.maintenance.model_dump()},
            "links": self.links.model_dump(),
            "services": [s.model_dump() for s in self.services],
            "mobility": {"bikeShare": [n.public() for n in self.mobility.bike_share]},
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
