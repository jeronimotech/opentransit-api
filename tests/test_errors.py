import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.cities import City
from app.errors import RouterUnavailable, install_error_handlers
from app.routers import plan, platform
from app.runtime import CityRuntime


class _FakeOtp:
    version = "test"

    async def graphql(self, *_a, **_k):
        raise RouterUnavailable("down")


def _app(bogota: City) -> FastAPI:
    from app.rt import RTCache
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(platform.router)
    app.include_router(plan.router)
    app.state.cities = {"bogota": CityRuntime(city=bogota, rt=RTCache(bogota), otp=_FakeOtp())}  # type: ignore[arg-type]
    return app


@pytest.mark.asyncio
async def test_city_not_found(bogota):
    async with AsyncClient(transport=ASGITransport(app=_app(bogota)), base_url="http://t") as c:
        r = await c.get("/v1/cities/medellin")
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "CITY_NOT_FOUND", "message": "unknown city 'medellin'"}}


@pytest.mark.asyncio
async def test_validation_error_envelope(bogota):
    async with AsyncClient(transport=ASGITransport(app=_app(bogota)), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/plan?fromLat=999&fromLon=0&toLat=0&toLon=0")
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "BAD_REQUEST" and "fromLat" in body["error"]["message"]


@pytest.mark.asyncio
async def test_router_unavailable_is_502(bogota):
    async with AsyncClient(transport=ASGITransport(app=_app(bogota)), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/plan?fromLat=4.7&fromLon=-74.0&toLat=4.6&toLon=-74.1")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "ROUTER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_unknown_mode_is_bad_request(bogota):
    async with AsyncClient(transport=ASGITransport(app=_app(bogota)), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/plan?fromLat=4.7&fromLon=-74.0&toLat=4.6&toLon=-74.1&modes=TELEPORT")
    assert r.status_code == 400
    assert r.json()["error"] == {"code": "BAD_REQUEST", "message": "unknown mode 'TELEPORT'"}


def test_parse_modes():
    import datetime as dt

    from app.routers.plan import build_variables, parse_modes
    assert parse_modes(None, ["BUS", "CABLE_CAR"]) == (["BUS", "CABLE_CAR"], ["WALK"])
    assert parse_modes("TRANSIT,BICYCLE", ["BUS"]) == (["BUS"], ["BICYCLE"])
    assert parse_modes("walk", ["BUS"]) == ([], ["WALK"])
    v = build_variables(from_lat=1, from_lon=2, to_lat=3, to_lon=4, when=dt.datetime(2026, 9, 4, 8, tzinfo=dt.UTC),
                        arrive_by=True, transit=["BUS"], street=["WALK"], wheelchair=True, num=3, locale="es",
                        walk_reluctance=2.0)
    assert v["dateTime"] == {"latestArrival": "2026-09-04T08:00:00+00:00"}
    assert v["modes"]["transit"]["transit"] == [{"mode": "BUS"}] and v["modes"]["direct"] == ["WALK"]
    assert v["modes"]["transit"]["access"] == ["WALK"]
    assert v["preferences"]["accessibility"]["wheelchair"]["enabled"] is True
    v2 = build_variables(from_lat=1, from_lon=2, to_lat=3, to_lon=4, when=dt.datetime(2026, 9, 4, 8, tzinfo=dt.UTC),
                         arrive_by=False, transit=[], street=["WALK"], wheelchair=False, num=3, locale="es",
                         walk_reluctance=None)
    assert v2["modes"] == {"direct": ["WALK"], "directOnly": True}
