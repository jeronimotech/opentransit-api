import hmac

from fastapi import APIRouter, Depends, Header, Query, Request

from ..admin_config import ConfigPatch, apply_to_runtime, deep_merge, describe, effective_city
from ..config import settings
from ..errors import Unauthorized
from ..gtfs_static import ingest, load_route_index, load_service_index
from ..normalize import set_feed_flags
from ..ondemand import mask_credentials, unmask_patch
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["admin"])


def require_admin(x_admin_token: str | None = Header(None)) -> None:
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings().ADMIN_TOKEN):
        raise Unauthorized("missing or invalid X-Admin-Token")


@router.post("/v1/admin/cities/{city}/ingest-static", dependencies=[Depends(require_admin)])
async def ingest_static(rt: CityRuntime = Depends(city_runtime), force: bool = False):
    result = await ingest(rt.city, force=force)
    rt.rt.set_static(*await load_route_index(rt.city))
    rt.services = await load_service_index(rt.city)
    set_feed_flags(rt.city.id, rt.services.flags)
    rt.static_ready = True
    return result


@router.post("/v1/admin/cities/{city}/purge", dependencies=[Depends(require_admin)])
async def purge(rt: CityRuntime = Depends(city_runtime)):
    """Drop in-memory vehicle history for this city (the only per-city state that grows)."""
    n = len(rt.rt.history)
    rt.rt.history.clear()
    return {"purgedVehicles": n}


# ------------------------------------------------------------------ editable city configuration
@router.get("/v1/admin/me", dependencies=[Depends(require_admin)])
async def admin_me(request: Request):
    """Token check for the admin UI login."""
    return {"ok": True, "cities": sorted(request.app.state.cities)}


@router.get("/v1/admin/cities/{city}/config", dependencies=[Depends(require_admin)])
async def get_config(rt: CityRuntime = Depends(city_runtime)):
    return describe(rt)


@router.put("/v1/admin/cities/{city}/config", dependencies=[Depends(require_admin)])
async def put_config(patch: ConfigPatch, request: Request, rt: CityRuntime = Depends(city_runtime)):
    """Partial deep-merge into the stored override. A JSON null for a section (or a key) removes that override
    so the YAML value applies again. The effective result is validated strictly before anything is saved."""
    sections = {k: v for k, v in patch.model_dump(exclude={"note", "updatedBy"}).items()
                if k in patch.model_fields_set}
    if sections.get("mobility"):
        # masked credentials echoed back by the UI keep their stored value; new values are stored as sent
        sections["mobility"] = unmask_patch(sections["mobility"], rt.city)
    new_override = deep_merge(rt.override or {}, sections)
    effective_city(rt.base_city or rt.city, new_override)          # raises 422 with the field path
    row = await request.app.state.config_store.save(rt.city.id, new_override, patch.updatedBy, patch.note)
    apply_to_runtime(rt, row)
    _sync_gbfs(rt)
    return describe(rt)


@router.delete("/v1/admin/cities/{city}/config", dependencies=[Depends(require_admin)])
async def delete_config(request: Request, rt: CityRuntime = Depends(city_runtime),
                        updatedBy: str | None = Query(None, max_length=120)):
    row = await request.app.state.config_store.clear(rt.city.id, updatedBy)
    apply_to_runtime(rt, row)
    _sync_gbfs(rt)
    return describe(rt)


@router.get("/v1/admin/cities/{city}/config/history", dependencies=[Depends(require_admin)])
async def config_history(request: Request, rt: CityRuntime = Depends(city_runtime),
                         limit: int = Query(20, ge=1, le=200)):
    items = await request.app.state.config_store.history(rt.city.id, limit)
    return {"items": [{**i, "data": mask_credentials(i.get("data"))} for i in items]}


def _sync_gbfs(rt: CityRuntime) -> None:
    from ..main import sync_gbfs  # local import: main imports this router
    sync_gbfs(rt)
