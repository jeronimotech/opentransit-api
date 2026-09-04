"""
Admin-editable city configuration.

The city YAML is the base. Admins may override a few sections (fares, client config, links, services,
primary colour) through `/v1/admin/cities/{city}/config`; the override is persisted, deep-merged on top
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

from .cities import AppConfig, City, Fares, Links, Maintenance, MinAppVersion, ServiceTile
from .db import pool
from .errors import ApiError

log = logging.getLogger("ot.admin_config")

EDITABLE = ("fares", "config", "links", "services", "branding")
SERVICE_ICONS = ("card", "report", "help", "link", "bike", "parking", "taxi", "ticket", "info", "map")


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


class ConfigPatch(BaseModel):
    """PUT body: any subset of the editable sections (JSON null removes that section's override)."""
    model_config = ConfigDict(extra="forbid")
    fares: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    links: dict[str, Any] | None = None
    services: list[dict[str, Any]] | None = None
    branding: dict[str, Any] | None = None
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
            "branding": {"primaryColor": pub["branding"]["primaryColor"]}}


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
                 "branding": _validate("branding", BrandingCfg, sections.get("branding") or {})}
    ids = [s["id"] for s in out["services"]]
    if len(ids) != len(set(ids)):
        raise ApiError("services: duplicate service id", status=422)
    return out


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
    return {"effective": rt.city.public(), "override": rt.override, "yaml": yaml_sections(base),
            "revision": rt.config_revision, "updatedAt": rt.config_updated_at, "updatedBy": rt.config_updated_by,
            "editable": list(EDITABLE)}
