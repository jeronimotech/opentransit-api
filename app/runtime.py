"""Per-city runtime objects (RT cache, OTP client) and the dependency that resolves `{city}`."""
import logging
from dataclasses import dataclass, field

from fastapi import Request

from .cities import City
from .errors import CityNotFound
from .features import ServiceIndex, now_in
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
    services: ServiceIndex = field(default_factory=ServiceIndex)
    # admin-editable config: `city` is the effective city, `base_city` the YAML one
    base_city: City | None = None
    override: dict | None = None
    config_revision: int = 0
    config_updated_at: str | None = None
    config_updated_by: str | None = None

    # ---- v1.1 helpers ----
    def service_window(self, route_id: str | None) -> dict | None:
        """Today's service window for a (scoped or raw) route id, or None when unknown."""
        if not route_id:
            return None
        return self.services.window_for(self.city.unscoped(route_id), now_in(self.city))

    def with_window(self, ref: dict | None) -> dict | None:
        if ref:
            ref["serviceWindow"] = self.service_window(ref.get("id"))
        return ref

    def freshness(self) -> dict:
        """How trustworthy the realtime layer is right now (used by board/next/health)."""
        import time
        c = self.rt
        f = self.city.feeds
        enabled = bool(f.rt_positions_url or f.rt_tripupdates_url or f.rt_alerts_url)
        age = c.health().get("entityAgeP50Seconds")
        since_fetch = int(time.time() - c.updated_at) if c.updated_at else None
        stale = (not enabled) or c.updated_at == 0 or (c.http_status not in (None, 200)) \
            or (age is not None and age > 90) or (since_fetch is not None and since_fetch > 90)
        return {"realtime": enabled and not stale, "ageSeconds": age, "staleSeconds": since_fetch,
                "stale": bool(stale)}


def city_runtime(request: Request, city: str) -> CityRuntime:
    rt = request.app.state.cities.get(city)
    if rt is None:
        raise CityNotFound(f"unknown city '{city}'")
    return rt
