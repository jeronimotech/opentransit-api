"""
v1.6 · Open Mobility Foundation endpoints (phase A).

Three groups:

* **Normalised (ours, camelCase)** — `/curbs`, `/curbs/nearby`, `/zones`. What the apps draw and explain.
* **Verbatim (spec-faithful, snake_case)** — `/cds/curbs/*` (CDS 1.1.0) and `/mds/policies|geographies`
  (MDS 2.1.0), so an operator can point their standard client at us. Envelopes, media types and the 406
  on an unsupported `Accept` version follow the specs.
* **Admin** — the curb inventory CRUD and the MDS document import.

Everything served here is the open data plane. Nothing in this router reads the restricted tables.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from ..errors import ApiError
from ..openmobility import (
    CDS_VERSION,
    MDS_VERSION,
    MEDIA_CDS,
    MEDIA_MDS,
    USER_CLASSES,
    ZONE_TYPES,
    CurbNotFound,
    OpenMobilityError,
    bbox_overlaps,
    cds_envelope,
    city_now,
    curb_public,
    distance_to_geometry_m,
    etag_for,
    geographies_public,
    geometry_bbox,
    mds_envelope,
    parse_curbs_document,
    parse_mds_documents,
    refresh_from_url,
    zones_public,
)
from ..routers.admin import require_admin
from ..runtime import CityRuntime, city_runtime

log = logging.getLogger("ot.openmobility")
router = APIRouter(tags=["openmobility"])

PUBLIC_CACHE = "public, max-age=3600"


def _store(request: Request):
    store = getattr(request.app.state, "openmobility_store", None)
    if store is None:
        raise OpenMobilityError("open mobility storage is not configured")
    return store


def _require_enabled(rt: CityRuntime) -> None:
    if not rt.city.open_mobility_enabled():
        raise OpenMobilityError(f"'{rt.city.id}' does not publish open-mobility data")


def _check_version(accept: str | None, media: str, spec: str) -> None:
    """The specs answer 406 when the client asks for a version we do not serve."""
    if not accept:
        return
    for part in accept.split(","):
        part = part.strip()
        if not part.startswith(media.split(";")[0]):
            continue
        for param in part.split(";")[1:]:
            k, _, v = param.strip().partition("=")
            if k.strip() == "version" and v.strip().strip('"') not in (spec, spec.rsplit(".", 1)[0]):
                raise ApiError(f"unsupported {media.split('+')[0].split('/')[-1].upper()} version "
                               f"'{v.strip()}'; this server speaks {spec}", code="NOT_ACCEPTABLE", status=406)


def _bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    parts = [float(x) for x in bbox.split(",")]
    return parts[0], parts[1], parts[2], parts[3]


def _spec_response(body: dict, media: str, *, cache: str = PUBLIC_CACHE) -> JSONResponse:
    tag = etag_for(body)
    return JSONResponse(body, media_type=media,
                        headers={"ETag": tag, "Cache-Control": cache,
                                 "Last-Modified": dt.datetime.now(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")})


# ------------------------------------------------------------------ normalised (ours)
@router.get("/v1/cities/{city}/curbs")
async def curbs(request: Request, rt: CityRuntime = Depends(city_runtime),
                bbox: str | None = Query(None, pattern=r"^-?[\d.]+(,-?[\d.]+){3}$"),
                lat: float | None = Query(None, ge=-90, le=90),
                lon: float | None = Query(None, ge=-180, le=180),
                userClass: str | None = Query(None),
                at: str | None = Query(None, description="ISO-8601; defaults to now in the city's timezone"),
                limit: int = Query(500, ge=1, le=5000)):
    """Curb zones with their regulations, evaluated against the city clock."""
    _require_enabled(rt)
    if userClass and userClass not in USER_CLASSES:
        raise ApiError(f"userClass: expected one of {', '.join(USER_CLASSES)}", status=422)
    zones, policies = await _store(request).curbs(rt.city.id)
    by_id = {str(p["curb_policy_id"]): p for p in policies}
    when = city_now(rt.city, at)
    box = _bbox(bbox)
    out = []
    for z in zones:
        if not bbox_overlaps(box, geometry_bbox(z.get("geometry"))):
            continue
        out.append(curb_public(z, by_id, when, rt.city, user_class=userClass, lat=lat, lon=lon))
    if lat is not None and lon is not None:
        out.sort(key=lambda c: (c.get("distanceMeters") is None, c.get("distanceMeters") or 0))
    return JSONResponse({"generatedAt": when.isoformat(), "count": len(out[:limit]), "total": len(zones),
                         "curbs": out[:limit]},
                        headers={"Cache-Control": PUBLIC_CACHE})


@router.get("/v1/cities/{city}/curbs/nearby")
async def curbs_nearby(request: Request, rt: CityRuntime = Depends(city_runtime),
                       lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                       radius: int = Query(300, ge=10, le=3000),
                       userClass: str | None = Query(None),
                       at: str | None = Query(None),
                       limit: int = Query(5, ge=1, le=50)):
    """The legal pick-up / drop-off zones around a point right now, nearest first.

    Zones whose winning policy forbids stopping are still returned (marked `allowed: false`) so an app can
    explain *why* a spot is not usable, but they always sort after the legal ones.
    """
    _require_enabled(rt)
    if userClass and userClass not in USER_CLASSES:
        raise ApiError(f"userClass: expected one of {', '.join(USER_CLASSES)}", status=422)
    zones, policies = await _store(request).curbs(rt.city.id)
    by_id = {str(p["curb_policy_id"]): p for p in policies}
    when = city_now(rt.city, at)
    scored = []
    for z in zones:
        d = distance_to_geometry_m(z.get("geometry"), lon, lat)
        if d is None or d > radius:
            continue
        item = curb_public(z, by_id, when, rt.city, user_class=userClass, lat=lat, lon=lon)
        # An explicit user class means "where may THIS vehicle stop": a zone whose policies never mention it
        # is not an answer. A zone that explicitly forbids it still is, so the app can say why.
        if userClass and not item["activePolicyIds"] and item["allowed"] is None:
            continue
        scored.append(item)
    scored.sort(key=lambda c: (c.get("allowed") is not True, c.get("distanceMeters") or 0))
    return JSONResponse({"generatedAt": when.isoformat(), "userClass": userClass, "radiusMeters": radius,
                         "count": len(scored[:limit]), "curbs": scored[:limit]},
                        headers={"Cache-Control": "public, max-age=60"})


@router.get("/v1/cities/{city}/zones")
async def zones(request: Request, rt: CityRuntime = Depends(city_runtime),
                type: str | None = Query(None, description="comma list: " + ",".join(ZONE_TYPES)),
                at: str | None = Query(None),
                activeOnly: bool = Query(False)):
    """MDS policy rules resolved against their geographies — what an app draws as 'do not park here'."""
    _require_enabled(rt)
    policies, geographies = await _store(request).mds(rt.city.id)
    when = city_now(rt.city, at)
    items = zones_public(policies, geographies, when, rt.city)
    if type:
        wanted = {t.strip() for t in type.split(",") if t.strip()}
        items = [z for z in items if z["type"] in wanted]
    if activeOnly:
        items = [z for z in items if z["active"]]
    return JSONResponse({"generatedAt": when.isoformat(), "count": len(items), "zones": items,
                         "geographies": geographies_public(geographies, when)},
                        headers={"Cache-Control": PUBLIC_CACHE})


# ------------------------------------------------------------------ verbatim CDS 1.1.0 Curbs API
@router.get("/v1/cities/{city}/cds/curbs/zones")
async def cds_zones(request: Request, rt: CityRuntime = Depends(city_runtime),
                    accept: str | None = Header(None)):
    _check_version(accept, MEDIA_CDS, CDS_VERSION)
    _require_publish(rt, "cds")
    zones, _ = await _store(request).curbs(rt.city.id)
    return _spec_response(cds_envelope(rt.city, "zones", zones), MEDIA_CDS)


@router.get("/v1/cities/{city}/cds/curbs/policies")
async def cds_policies(request: Request, rt: CityRuntime = Depends(city_runtime),
                       accept: str | None = Header(None)):
    _check_version(accept, MEDIA_CDS, CDS_VERSION)
    _require_publish(rt, "cds")
    _, policies = await _store(request).curbs(rt.city.id)
    return _spec_response(cds_envelope(rt.city, "policies", policies), MEDIA_CDS)


@router.get("/v1/cities/{city}/cds/curbs/areas")
async def cds_areas(request: Request, rt: CityRuntime = Depends(city_runtime),
                    accept: str | None = Header(None)):
    """Optional in CDS; we serve an empty, well-formed collection so a client's discovery does not break."""
    _check_version(accept, MEDIA_CDS, CDS_VERSION)
    _require_publish(rt, "cds")
    return _spec_response(cds_envelope(rt.city, "areas", []), MEDIA_CDS)


# ------------------------------------------------------------------ verbatim MDS 2.1.0 Policy / Geography
@router.get("/v1/cities/{city}/mds/policies")
async def mds_policies(request: Request, rt: CityRuntime = Depends(city_runtime),
                       accept: str | None = Header(None)):
    _check_version(accept, MEDIA_MDS, MDS_VERSION)
    _require_publish(rt, "mds")
    policies, _ = await _store(request).mds(rt.city.id)
    return _spec_response(mds_envelope("policies", policies), MEDIA_MDS)


@router.get("/v1/cities/{city}/mds/geographies")
async def mds_geographies(request: Request, rt: CityRuntime = Depends(city_runtime),
                          accept: str | None = Header(None)):
    _check_version(accept, MEDIA_MDS, MDS_VERSION)
    _require_publish(rt, "mds")
    _, geographies = await _store(request).mds(rt.city.id)
    return _spec_response(mds_envelope("geographies", geographies), MEDIA_MDS)


def _require_publish(rt: CityRuntime, which: str) -> None:
    om = rt.city.open_mobility
    ok = (om.cds.enabled and om.cds.publish) if which == "cds" else (om.mds.enabled and om.mds.publish_policy)
    if not ok:
        raise OpenMobilityError(f"'{rt.city.id}' does not publish {which.upper()} data")


# ------------------------------------------------------------------ admin: curb inventory + MDS import
@router.get("/v1/admin/cities/{city}/curbs", dependencies=[Depends(require_admin)])
async def admin_curbs(request: Request, rt: CityRuntime = Depends(city_runtime)):
    zones, policies = await _store(request).curbs(rt.city.id)
    return {"zones": zones, "policies": policies, "count": len(zones)}


@router.put("/v1/admin/cities/{city}/curbs", dependencies=[Depends(require_admin)])
async def put_curbs(request: Request, rt: CityRuntime = Depends(city_runtime),
                    replace: bool = Query(False, description="replace the whole inventory instead of upserting"),
                    body: Any = Body(...)):
    """Load a CDS Curbs document, a `{zones, policies}` pair, or a GeoJSON FeatureCollection whose
    properties carry the CDS fields. Ids that are not UUIDs are derived deterministically."""
    zones, policies = parse_curbs_document(body)
    known = {str(p["curb_policy_id"]) for p in policies}
    if not replace:
        _, existing = await _store(request).curbs(rt.city.id)
        known |= {str(p["curb_policy_id"]) for p in existing}
    for z in zones:
        missing = [p for p in z["curb_policy_ids"] if p not in known]
        if missing:
            raise ApiError(f"curb zone {z['curb_zone_id']}: unknown curb_policy_id {missing[0]}", status=422)
    result = await _store(request).put_curbs(rt.city.id, zones, policies, replace=replace)
    return {"ok": True, **result, "replaced": replace}


@router.delete("/v1/admin/cities/{city}/curbs", dependencies=[Depends(require_admin)])
async def delete_curbs(request: Request, rt: CityRuntime = Depends(city_runtime),
                       zoneId: str | None = Query(None)):
    """Delete one zone, or the whole inventory when `zoneId` is omitted."""
    store = _store(request)
    if zoneId:
        if not await store.delete_curb(rt.city.id, zoneId):
            raise CurbNotFound(f"unknown curb zone '{zoneId}'")
        return {"ok": True, "deleted": 1}
    return {"ok": True, "deleted": await store.clear_curbs(rt.city.id)}


@router.put("/v1/admin/cities/{city}/mds/documents", dependencies=[Depends(require_admin)])
async def put_mds_documents(request: Request, rt: CityRuntime = Depends(city_runtime),
                            replace: bool = Query(False),
                            body: Any = Body(...)):
    """Load MDS Policy and/or Geography documents (a policy response, a geography response, or a bundle)."""
    policies, geographies = parse_mds_documents(body)
    known = {str(g["geography_id"]) for g in geographies}
    if not replace:
        _, existing = await _store(request).mds(rt.city.id)
        known |= {str(g["geography_id"]) for g in existing}
    for p in policies:
        for r in p.get("rules") or []:
            missing = [g for g in r.get("geographies") or [] if g not in known]
            if missing:
                raise ApiError(f"policy {p['policy_id']}: unknown geography {missing[0]}", status=422)
    result = await _store(request).put_mds(rt.city.id, policies, geographies, replace=replace)
    return {"ok": True, **result, "replaced": replace}


@router.post("/v1/admin/cities/{city}/openmobility/refresh", dependencies=[Depends(require_admin)])
async def refresh(request: Request, rt: CityRuntime = Depends(city_runtime),
                  kind: str = Query("cds", pattern="^(cds|mds)$")):
    """Pull the configured third-party feed now (CDS Curbs URL or MDS authority URL)."""
    om = rt.city.open_mobility
    url = om.cds.curbs.url if kind == "cds" else om.mds.authority_url
    if not url:
        raise ApiError(f"openMobility.{kind}: no source URL configured", status=422)
    try:
        result = await refresh_from_url(_store(request), rt.city, url, kind=kind)
    except Exception as e:  # noqa: BLE001
        raise ApiError(f"could not refresh from {url}: {e}", status=502, code="UPSTREAM_ERROR") from None
    return {"ok": True, "kind": kind, "url": url, **result}
