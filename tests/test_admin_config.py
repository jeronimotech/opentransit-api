"""Admin-editable city configuration: merge semantics, validation envelope, fare propagation, history, auth."""
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admin_config import MemoryConfigStore, deep_merge, effective_city
from app.cities import City
from app.errors import ApiError, install_error_handlers
from app.features import estimate_fare
from app.routers import admin, platform
from app.rt import RTCache
from app.runtime import CityRuntime

H = {"X-Admin-Token": "test-token"}


def _app(bogota: City) -> tuple[FastAPI, CityRuntime]:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(platform.router)
    app.include_router(admin.router)
    rt = CityRuntime(city=bogota, rt=RTCache(bogota), otp=None)  # type: ignore[arg-type]
    app.state.cities = {"bogota": rt}
    app.state.config_store = MemoryConfigStore()
    return app, rt


def _leg(start: str, short: str = "G12") -> dict:
    return {"transit": True, "startTime": start, "route": {"shortName": short}}


def test_deep_merge_null_removes_and_lists_replace():
    base = {"fares": {"base": 3200, "transfer": 0}, "services": [{"id": "a"}], "links": {"pqrs": "x"}}
    out = deep_merge(base, {"fares": {"base": 3400}, "services": [{"id": "b"}], "links": None})
    assert out == {"fares": {"base": 3400, "transfer": 0}, "services": [{"id": "b"}]}
    assert deep_merge(out, {"fares": {"base": None}}) == {"fares": {"transfer": 0}, "services": [{"id": "b"}]}


def test_effective_city_applies_override_and_keeps_yaml_defaults(bogota: City):
    city = effective_city(bogota, {"fares": {"base": 3400}, "branding": {"primaryColor": "#123456"}})
    assert city.fares.base == 3400 and city.fares.transfer_window_minutes == 110
    assert city.branding.primary_color == "#123456"
    assert city.feeds == bogota.feeds and city.otp == bogota.otp       # never editable
    assert effective_city(bogota, None).public() == bogota.public()


def test_effective_city_validation_paths(bogota: City):
    with pytest.raises(ApiError) as e:
        effective_city(bogota, {"fares": {"base": -1}})
    assert e.value.status == 422 and e.value.message.startswith("fares.base:")
    with pytest.raises(ApiError) as e:
        effective_city(bogota, {"links": {"pqrs": "http://insecure"}})
    assert e.value.message.startswith("links.pqrs:")
    with pytest.raises(ApiError) as e:
        effective_city(bogota, {"services": [{"id": "Bad Id", "label": "x", "url": "https://x"}]})
    assert e.value.message.startswith("services.0.id:")
    with pytest.raises(ApiError) as e:
        effective_city(bogota, {"config": {"minAppVersion": {"ios": "1.0"}}})
    assert e.value.message.startswith("config.minAppVersion.ios:")


async def test_requires_admin_token(bogota: City):
    app, _ = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/admin/cities/bogota/config")
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
        r = await c.get("/v1/admin/me", headers={"X-Admin-Token": "wrong"})
        assert r.status_code == 401
        r = await c.get("/v1/admin/me", headers=H)
        assert r.json() == {"ok": True, "cities": ["bogota"]}


async def test_put_propagates_to_public_city_and_fares(bogota: City):
    app, rt = _app(bogota)
    legs = [_leg("2026-09-04T08:00:00-05:00"), _leg("2026-09-04T08:30:00-05:00", "TC14")]
    assert estimate_fare(rt.city, legs)["amount"] == 3200
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/admin/cities/bogota/config", headers=H)
        assert r.status_code == 200
        body = r.json()
        assert body["override"] is None and body["revision"] == 0 and body["yaml"]["fares"]["base"] == 3200
        assert body["editable"] == ["fares", "config", "links", "services", "branding", "mobility"]

        r = await c.put("/v1/admin/cities/bogota/config", headers=H,
                        json={"fares": {"base": 3400, "transfer": 200}, "note": "tarifa 2027", "updatedBy": "luis"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["revision"] == 1 and body["updatedBy"] == "luis"
        assert body["override"] == {"fares": {"base": 3400, "transfer": 200}}
        assert body["effective"]["fares"]["base"] == 3400 and body["effective"]["fares"]["currency"] == "COP"

        pub = await c.get("/v1/cities/bogota")
        assert pub.json()["fares"]["base"] == 3400
        assert pub.headers["cache-control"] == "public, max-age=60"
        assert (await c.get("/v1/cities")).json()["cities"][0]["fares"]["transfer"] == 200
    fare = estimate_fare(rt.city, legs)
    assert fare["amount"] == 3600 and [b["amount"] for b in fare["breakdown"]] == [3400, 200]


async def test_null_removes_override_key_and_delete_resets(bogota: City):
    app, rt = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.put("/v1/admin/cities/bogota/config", headers=H,
                    json={"fares": {"base": 3400}, "config": {"maintenance": {"active": True, "message": "hoy"}}})
        r = await c.put("/v1/admin/cities/bogota/config", headers=H, json={"fares": None})
        body = r.json()
        assert "fares" not in body["override"] and body["effective"]["fares"]["base"] == 3200
        assert body["effective"]["config"]["maintenance"] == {"active": True, "message": "hoy"}
        assert body["revision"] == 2

        r = await c.delete("/v1/admin/cities/bogota/config?updatedBy=luis", headers=H)
        body = r.json()
        assert body["override"] is None and body["revision"] == 3
        assert body["effective"]["config"]["maintenance"]["active"] is False
        assert rt.city.config.maintenance.active is False

        h = (await c.get("/v1/admin/cities/bogota/config/history?limit=10", headers=H)).json()["items"]
        assert [x["revision"] for x in h] == [3, 2, 1]
        assert h[0]["note"] == "reset" and h[0]["changedBy"] == "luis"
        assert h[2]["data"]["fares"]["base"] == 3400


async def test_invalid_put_is_rejected_without_saving(bogota: City):
    app, rt = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/v1/admin/cities/bogota/config", headers=H, json={"fares": {"maxTransfers": 9}})
        assert r.status_code == 422
        assert r.json()["error"] == {"code": "BAD_REQUEST",
                                     "message": "fares.maxTransfers: Input should be less than or equal to 5"}
        r = await c.put("/v1/admin/cities/bogota/config", headers=H, json={"feeds": {"gtfs_static_url": "x"}})
        assert r.status_code == 422 and "feeds" in r.json()["error"]["message"]
        r = await c.put("/v1/admin/cities/bogota/config", headers=H,
                        json={"services": [{"id": "a", "label": "A", "url": "https://a"},
                                           {"id": "a", "label": "B", "url": "https://b"}]})
        assert r.status_code == 422 and "duplicate" in r.json()["error"]["message"]
        assert (await c.get("/v1/admin/cities/bogota/config/history", headers=H)).json()["items"] == []
    assert rt.override is None and rt.city.fares.base == 3200
