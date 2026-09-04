"""Per-city runtime objects (RT cache, OTP client) and the dependency that resolves `{city}`."""
import logging
from dataclasses import dataclass, field

from fastapi import Request

from .cities import City
from .errors import CityNotFound
from .otp import OtpClient
from .rt import RTCache

log = logging.getLogger("ot.runtime")


@dataclass
class CityRuntime:
    city: City
    rt: RTCache
    otp: OtpClient
    static_ready: bool = False
    ingest_error: str | None = None
    meta: dict = field(default_factory=dict)


def city_runtime(request: Request, city: str) -> CityRuntime:
    rt = request.app.state.cities.get(city)
    if rt is None:
        raise CityNotFound(f"unknown city '{city}'")
    return rt
