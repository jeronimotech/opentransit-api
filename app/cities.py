"""City (tenant) registry. Loaded once from cities/*.yaml; `${VAR}` / `${VAR:-default}` are expanded."""
import logging
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("ot.cities")

Component = Literal["trunk", "feeder", "dual", "zonal", "cable", "rail", "other"]
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(text: str) -> str:
    return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), text)


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
            "attribution": self.attribution,
        }


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
