"""v2.3 — anonymous device registration for scheduled-trip pushes (iOS)."""
import datetime as dt

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from ..errors import ApiError
from ..push import normalize_registration
from ..runtime import CityRuntime, city_runtime

router = APIRouter(tags=["platform"])
NO_STORE = {"Cache-Control": "no-store"}


@router.put("/v1/cities/{city}/push/devices", status_code=202)
async def register_device(request: Request, rt: CityRuntime = Depends(city_runtime), body: dict = Body(...)):
    """A phone registers (or re-registers, idempotently) its APNs token with the instants it wants to be
    woken at and the routes it follows. Nothing else: no account, no position, no trip. With server
    reminders off for the city the request is accepted and ignored, so clients need no branching."""
    try:
        device = normalize_registration(body, city=rt.city.id, now=dt.datetime.now(dt.UTC))
    except ValueError as e:
        raise ApiError(str(e), status=422) from e
    if not rt.city.config.push.reminders_active:
        return JSONResponse({"accepted": True, "serverPush": False, "reason": "server reminders disabled"},
                            status_code=202, headers=NO_STORE)
    await request.app.state.push_devices.upsert(device)
    return JSONResponse({"accepted": True, "serverPush": True, "wakes": len(device["wake_at"]),
                         "routes": len(device["routes"])}, status_code=202, headers=NO_STORE)


@router.delete("/v1/cities/{city}/push/devices/{token}", status_code=204)
async def unregister_device(token: str, request: Request, rt: CityRuntime = Depends(city_runtime)):
    store = getattr(request.app.state, "push_devices", None)
    if store is not None:
        await store.delete(token.lower())
    return JSONResponse(None, status_code=204, headers=NO_STORE)
