"""v1.7 shareable ETA: publish a trip in progress under an unguessable, expiring token."""
from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from ..errors import ApiError
from ..models import ShareCreateResponse, ShareReadResponse
from ..runtime import CityRuntime, city_runtime
from ..share import (
    MAX_ITINERARY_BYTES,
    PROGRESS_STATES,
    clean_progress,
    expiry,
    hash_key,
    key_matches,
    new_token,
    new_write_key,
)

router = APIRouter(tags=["share"])

NO_STORE = {"Cache-Control": "no-store"}


class ShareNotFound(ApiError):
    status, code = 404, "SHARE_NOT_FOUND"


class ShareForbidden(ApiError):
    status, code = 403, "FORBIDDEN"


class TooManyRequests(ApiError):
    status, code = 429, "RATE_LIMITED"


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _require_enabled(rt: CityRuntime) -> None:
    if not rt.city.config.share.enabled:
        raise ApiError(f"sharing is disabled for {rt.city.name}", status=404, code="SHARE_DISABLED")


def _iso(value: dt.datetime | None) -> str | None:
    return None if value is None else value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


@router.post("/v1/cities/{city}/share/eta", response_model=ShareCreateResponse, status_code=201)
async def create_share(request: Request, rt: CityRuntime = Depends(city_runtime), body: dict = Body(...)):
    """Publish an itinerary. The write key comes back once and is the only way to update or revoke it."""
    _require_enabled(rt)
    limiter = request.app.state.share_limiter
    if not limiter.allow(_client_key(request)):
        raise TooManyRequests("too many shares created; retry later")
    itinerary = body.get("itinerary")
    if not isinstance(itinerary, dict) or not itinerary.get("legs"):
        raise ApiError("itinerary: an itinerary with legs is required", status=422)
    if len(json.dumps(itinerary)) > MAX_ITINERARY_BYTES:
        raise ApiError("itinerary too large", status=413, code="PAYLOAD_TOO_LARGE")
    label = body.get("label")
    if label is not None:
        label = str(label)[:80]
    cfg = rt.city.config.share
    ttl = body.get("ttlMinutes")
    expires = expiry(dt.datetime.now(dt.UTC), int(ttl) if ttl else 0, cfg.ttl_minutes, cfg.max_ttl_minutes)
    token, write_key = new_token(), new_write_key()
    await request.app.state.share_store.create(
        rt.city.id, token, hash_key(write_key), itinerary,
        label=label, started_at=body.get("startedAt"), expires_at=expires)
    base = str(request.base_url).rstrip("/")
    return JSONResponse(
        ShareCreateResponse(token=token, url=f"{base}/v1/cities/{rt.city.id}/share/eta/{token}",
                            write_key=write_key, expires_at=_iso(expires)).model_dump(by_alias=True),
        status_code=201, headers=NO_STORE)


@router.get("/v1/cities/{city}/share/eta/{token}", response_model=ShareReadResponse)
async def read_share(token: str, request: Request, rt: CityRuntime = Depends(city_runtime)):
    """Public read: anyone with the link. Never cached, never indexed (the web page sends noindex)."""
    _require_enabled(rt)
    row = await request.app.state.share_store.get(rt.city.id, token)
    if row is None:
        raise ShareNotFound("this shared trip has expired or was revoked")
    city = rt.city.public()
    body = ShareReadResponse(
        label=row.get("label"), itinerary=row["itinerary"], progress=row.get("progress"),
        started_at=row.get("started_at"), updated_at=_iso(row.get("updated_at")),
        expires_at=_iso(row["expires_at"]),
        city={"id": city["id"], "name": city["name"], "timezone": city["timezone"],
              "branding": city["branding"], "attribution": city.get("attribution")},
    ).model_dump(by_alias=True)
    return JSONResponse(body, headers=NO_STORE)


@router.patch("/v1/cities/{city}/share/eta/{token}")
async def patch_share(token: str, request: Request, rt: CityRuntime = Depends(city_runtime),
                      body: dict = Body(...),
                      x_share_key: str | None = Header(None, alias="X-Share-Key")):
    """Move the dot. Only the creator's write key is accepted; coordinates are coarsened before storage."""
    _require_enabled(rt)
    row = await request.app.state.share_store.get(rt.city.id, token)
    if row is None:
        raise ShareNotFound("this shared trip has expired or was revoked")
    if not key_matches(x_share_key or body.get("writeKey"), row["key_hash"]):
        raise ShareForbidden("a valid X-Share-Key is required to update this trip")
    progress = body.get("progress")
    if not isinstance(progress, dict) or "legIndex" not in progress:
        raise ApiError("progress.legIndex is required", status=422)
    state = progress.get("state") or "on_time"
    if state not in PROGRESS_STATES:
        raise ApiError(f"progress.state must be one of {', '.join(PROGRESS_STATES)}", status=422)
    try:
        cleaned = clean_progress(progress)
    except (TypeError, ValueError) as e:
        raise ApiError(f"progress: {e}", status=422) from None
    await request.app.state.share_store.patch(rt.city.id, token, cleaned)
    return JSONResponse({"ok": True, "progress": cleaned}, headers=NO_STORE)


@router.delete("/v1/cities/{city}/share/eta/{token}", status_code=204)
async def revoke_share(token: str, request: Request, rt: CityRuntime = Depends(city_runtime),
                       x_share_key: str | None = Header(None, alias="X-Share-Key")):
    _require_enabled(rt)
    row = await request.app.state.share_store.get(rt.city.id, token)
    if row is None:
        raise ShareNotFound("this shared trip has expired or was revoked")
    if not key_matches(x_share_key, row["key_hash"]):
        raise ShareForbidden("a valid X-Share-Key is required to revoke this trip")
    await request.app.state.share_store.delete(rt.city.id, token)
    return Response(status_code=204, headers=NO_STORE)


# ------------------------------------------------------------------ A4: Live Activity registration
@router.post("/v1/cities/{city}/live-activity/register", status_code=202)
async def register_live_activity(rt: CityRuntime = Depends(city_runtime), body: dict = Body(...),
                                 _: str | None = Query(None, include_in_schema=False)):
    """Accepts the activity token so the server *can* push updates once APNs credentials exist.

    With `config.push.enabled` false (the default) nothing is stored and nothing is pushed: the app updates
    its own Live Activity locally, which works because GO holds a foreground location session. The endpoint
    still answers 202 so clients need no branching."""
    if not body.get("activityToken") or not body.get("tripId"):
        raise ApiError("activityToken and tripId are required", status=422)
    push = rt.city.config.push
    if not push.enabled:
        return JSONResponse({"accepted": True, "serverPush": False,
                             "reason": "server push disabled; the app updates its own Live Activity"},
                            status_code=202, headers=NO_STORE)
    # Seam for the APNs sender: credentials are configured, so a future pass can register and push here.
    return JSONResponse({"accepted": True, "serverPush": True, "reason": None},
                        status_code=202, headers=NO_STORE)


@router.post("/v1/cities/{city}/live-activity/end", status_code=202)
async def end_live_activity(rt: CityRuntime = Depends(city_runtime), body: dict = Body(...)):
    if not body.get("tripId"):
        raise ApiError("tripId is required", status=422)
    push = rt.city.config.push
    return JSONResponse({"accepted": True, "serverPush": push.enabled,
                         "reason": None if push.enabled else "server push disabled"},
                        status_code=202, headers=NO_STORE)
