"""White-label landing (v1.3): validation bounds, effective merge + fallbacks, endpoint stats shape, disabled → 404."""
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admin_config import MemoryConfigStore, effective_city
from app.cities import City
from app.errors import ApiError, install_error_handlers
from app.routers import admin, landing
from app.rt import RTCache
from app.runtime import CityRuntime

H = {"X-Admin-Token": "test-token"}


def _app(bogota: City) -> tuple[FastAPI, CityRuntime]:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(landing.router)
    app.include_router(admin.router)
    rt = CityRuntime(city=bogota, rt=RTCache(bogota), otp=None)  # type: ignore[arg-type]
    app.state.cities = {"bogota": rt}
    app.state.config_store = MemoryConfigStore()
    return app, rt


def test_yaml_landing_and_fallbacks(bogota: City):
    ld = bogota.landing_public()
    assert ld["enabled"] and len(ld["highlights"]) == 5 and len(ld["screenshots"]) == 6
    assert ld["theme"]["primaryColor"] == bogota.branding.primary_color          # null -> branding
    assert ld["footer"]["privacyUrl"] == bogota.links.privacy                   # null -> links.privacy
    assert ld["footer"]["attribution"] == bogota.attribution
    assert ld["locale"] == "es"
    # open-data links derive from the feeds + bike-share when the list is empty
    city = effective_city(bogota, {"landing": {"openData": {"links": []}}})
    links = city.landing_public()["openData"]["links"]
    assert links[0]["url"] == bogota.feeds.gtfs_static_url
    assert any(x["url"] == bogota.mobility.bike_share[0].gbfs_url for x in links)


def test_effective_merge_keeps_yaml_defaults(bogota: City):
    city = effective_city(bogota, {"landing": {"hero": {"title": "Hola"}, "enabled": False}})
    assert city.landing.hero.title == "Hola"
    assert city.landing.hero.subtitle == bogota.landing.hero.subtitle
    assert city.landing.enabled is False
    assert len(city.landing.highlights) == 5


@pytest.mark.parametrize("patch,path", [
    ({"highlights": [{"title": "x"}] * 9}, "landing.highlights"),
    ({"hero": {"title": "t" * 81}}, "landing.hero.title"),
    ({"faq": [{"q": "q", "a": "a" * 601}]}, "landing.faq.0.a"),
    ({"screenshots": [{"url": "http://insecure/x.png"}]}, "landing.screenshots.0.url"),
    ({"highlights": [{"icon": "rocket", "title": "x"}]}, "landing.highlights.0.icon"),
    ({"contact": {"email": "not-an-email"}}, "landing.contact.email"),
    ({"feeds_url": "x"}, "landing.feeds_url"),
])
def test_validation_bounds(bogota: City, patch: dict, path: str):
    with pytest.raises(ApiError) as e:
        effective_city(bogota, {"landing": patch})
    assert e.value.status == 422 and e.value.message.startswith(path + ":")


def test_cta_url_accepts_anchor_and_path(bogota: City):
    city = effective_city(bogota, {"landing": {"hero": {"ctaSecondary": {"label": "Ir", "url": "/bogota"}}}})
    assert city.landing.hero.cta_secondary.url == "/bogota"


async def test_endpoint_shape_stats_and_cache(bogota: City):
    app, rt = _app(bogota)
    rt.rt.vehicles = [{"id": "v1"}, {"id": "v2"}]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/landing")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=300"
        body = r.json()
        assert set(body) == {"city", "landing", "stats", "apps"}
        assert set(body["stats"]) == {"routes", "stops", "vehiclesLive", "bikeStations", "alertsActive",
                                      "generatedAt"}
        assert body["stats"]["vehiclesLive"] == 0            # RT never fetched -> stale -> not advertised
        assert body["stats"]["alertsActive"] == 0
        assert body["city"]["id"] == "bogota" and body["city"]["mobility"]["bikeShare"][0]["id"]
        assert "gbfsUrl" not in body["city"]["mobility"]["bikeShare"][0]
        assert body["landing"]["hero"]["title"].startswith("Muévete")
        assert body["apps"] == {"ios": None, "android": None, "web": None}
        # stats are cached for 60 s
        rt.rt.vehicles = []
        assert (await c.get("/v1/cities/bogota/landing")).json()["stats"] == body["stats"]


async def test_admin_toggle_disables_landing(bogota: City):
    app, _ = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/v1/admin/cities/bogota/config", headers=H,
                        json={"landing": {"enabled": False}, "note": "off"})
        assert r.status_code == 200 and r.json()["override"] == {"landing": {"enabled": False}}
        assert "landing" in r.json()["editable"]
        r = await c.get("/v1/cities/bogota/landing")
        assert r.status_code == 404 and r.json()["error"]["code"] == "LANDING_DISABLED"
        r = await c.delete("/v1/admin/cities/bogota/config", headers=H)
        assert r.status_code == 200
        assert (await c.get("/v1/cities/bogota/landing")).status_code == 200
