"""On-demand mobility (v1.4): hand-off links, credential masking, public shapes, estimate endpoint, plan merge,
admin validation. Everything is driven by cities/bogota.yaml config; no provider is named in app code."""
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admin_config import MemoryConfigStore, effective_city
from app.cities import City, OnDemandHandoff, OnDemandProvider
from app.errors import ApiError, install_error_handlers
from app.normalize import plan_from_otp
from app.ondemand import (
    CarRouter,
    attach_to_plan,
    build_handoff,
    is_masked,
    mask_credentials,
    unmask_patch,
)
from app.routers import admin, ondemand
from app.routers.plan import build_variables, merge_ondemand
from app.rt import RTCache
from app.runtime import CityRuntime

FIX = Path(__file__).parent / "fixtures"
H = {"X-Admin-Token": "test-token"}
TZ = ZoneInfo("America/Bogota")
WHEN = dt.datetime(2026, 9, 8, 10, tzinfo=TZ)


def _app(city: City) -> tuple[FastAPI, CityRuntime]:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ondemand.router)
    app.include_router(admin.router)
    rt = CityRuntime(city=city, rt=RTCache(city), otp=None)  # type: ignore[arg-type]
    app.state.cities = {"bogota": rt}
    app.state.config_store = MemoryConfigStore()
    return app, rt


class FakeOtp:
    """Stands in for OtpClient: every GraphQL call returns the captured direct-car response."""
    def __init__(self):
        self.calls = 0
        self.data = json.loads((FIX / "otp_plan_car.json").read_text(encoding="utf-8"))

    async def graphql(self, query, variables=None, locale=None):
        self.calls += 1
        return self.data


# ------------------------------------------------------------------ config + public shapes
def test_yaml_no_provider_names_in_code():
    app_dir = Path(__file__).resolve().parent.parent / "app"
    hits = [p for p in app_dir.rglob("*.py")
            if any(w in p.read_text(encoding="utf-8").lower() for w in ("uber", "cabify", "didi", "indrive"))]
    assert hits == [], f"provider names leaked into code: {hits}"


def test_public_city_has_no_credentials(bogota: City):
    pub = bogota.public()
    assert pub["features"]["onDemand"] is True
    assert "credentials" not in json.dumps(pub)
    assert [p["id"] for p in pub["mobility"]["onDemand"]] == ["taxi", "uber", "cabify", "didi", "indrive"]
    uber = next(p for p in pub["mobility"]["onDemand"] if p["id"] == "uber")
    assert uber["handoff"]["hasTemplate"] is True and "template" not in uber["handoff"]


# ------------------------------------------------------------------ hand-off links
def _provider(**kw) -> OnDemandProvider:
    base = dict(id="ride", name="Ride", kind="ridehail",
                handoff=OnDemandHandoff(kind="template",
                                        template="https://x.example/go?client_id={clientId}&pickup={pickupJson}"
                                                 "&drop[0]={dropoffJson}&to={dropoffName}",
                                        web="https://x.example/", apps={"ios": "https://apps/x", "android": None}),
                credentials={"clientId": "abc-123"})
    base.update(kw)
    return OnDemandProvider(**base)


def test_template_injects_and_encodes():
    out = build_handoff(_provider(), from_lat=4.6, from_lon=-74.1, to_lat=4.5, to_lon=-74.2,
                        from_name="Parque 93", to_name="Portal Sur & Co", platform="web")
    assert out["kind"] == "template" and out["fallback"] == "https://x.example/"
    url = out["url"]
    assert url.startswith("https://x.example/go?client_id=abc-123&pickup=%7B%22latitude%22%3A4.6")
    assert "addressLine1%22%3A%22Parque%2093%22" in url and url.endswith("&to=Portal%20Sur%20%26%20Co")
    assert "{" not in url and "abc-123" in url


def test_missing_credential_falls_back_instead_of_broken_link():
    out = build_handoff(_provider(credentials={}), from_lat=4.6, from_lon=-74.1, to_lat=4.5, to_lon=-74.2,
                        platform="ios")
    assert out["kind"] == "url" and out["url"] == "https://apps/x" and out["missingCredentials"] == ["clientId"]
    # url-kind providers just pick the best link for the platform; android without a store link -> web
    p = _provider(handoff=OnDemandHandoff(kind="url", web="https://x.example/", apps={"ios": "https://apps/x"}))
    assert build_handoff(p, from_lat=0, from_lon=0, to_lat=1, to_lon=1, platform="android")["url"] == "https://x.example/"
    none = _provider(handoff=OnDemandHandoff(kind="none", web="https://x.example/"))
    assert build_handoff(none, from_lat=0, from_lon=0, to_lat=1, to_lon=1)["url"] is None


# ------------------------------------------------------------------ masking
def test_mask_and_unmask_credentials(bogota: City):
    prov = {"id": "uber", "name": "Uber", "kind": "ridehail", "estimate": {"kind": "none"},
            "handoff": {"kind": "template", "template": "https://x.example/?c={clientId}"}, "order": 2}
    data = {"mobility": {"onDemand": [{**prov, "credentials": {"clientId": "secret-client-id-9f3a"}}]}}
    masked = mask_credentials(data)
    v = masked["mobility"]["onDemand"][0]["credentials"]["clientId"]
    assert is_masked(v) and v.endswith("9f3a") and "secret" not in v
    assert data["mobility"]["onDemand"][0]["credentials"]["clientId"].startswith("secret")   # input untouched
    # a masked value echoed back keeps the stored one; a masked value for an unknown provider is dropped
    city = effective_city(bogota, data["mobility"] and {"mobility": data["mobility"]})
    patch = {"onDemand": [{"id": "uber", "credentials": {"clientId": v}},
                          {"id": "new", "credentials": {"clientId": "••••zzzz"}}]}
    out = unmask_patch(patch, city)
    assert out["onDemand"][0]["credentials"]["clientId"] == "secret-client-id-9f3a"
    assert out["onDemand"][1]["credentials"] == {}


async def test_admin_masks_in_get_and_history_and_public_stays_clean(bogota: City):
    app, rt = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/v1/admin/cities/bogota/config", headers=H,
                        json={"mobility": {"onDemand": [
                            {"id": "uber", "name": "Uber", "kind": "ridehail", "estimate": {"kind": "none"},
                             "handoff": {"kind": "template",
                                         "template": "https://m.uber.com/looking?client_id={clientId}&pickup={pickupJson}",
                                         "apps": {"ios": None, "android": None}},
                             "credentials": {"clientId": "real-client-id-1a2b"}, "order": 2}]}})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["override"]["mobility"]["onDemand"][0]["credentials"]["clientId"] == "••••1a2b"
        assert "credentials" not in json.dumps(body["effective"])
        assert "real-client" not in json.dumps(body)
        h = (await c.get("/v1/admin/cities/bogota/config/history", headers=H)).json()
        hs = json.dumps(h, ensure_ascii=False)
        assert "real-client" not in hs and "••••1a2b" in hs
        # the stored value is the real one and the hand-off uses it
        assert rt.city.on_demand_provider("uber").credentials["clientId"] == "real-client-id-1a2b"
        r = await c.get("/v1/cities/bogota/ondemand/handoff", params={"providerId": "uber", "fromLat": 4.6,
                                                                       "fromLon": -74.1, "toLat": 4.5, "toLon": -74.2})
        assert r.json()["url"].startswith("https://m.uber.com/looking?client_id=real-client-id-1a2b")
        # echoing the masked value back keeps the real credential
        r = await c.put("/v1/admin/cities/bogota/config", headers=H,
                        json={"mobility": {"onDemand": [
                            {"id": "uber", "name": "Uber", "kind": "ridehail", "estimate": {"kind": "none"},
                             "handoff": {"kind": "template",
                                         "template": "https://m.uber.com/looking?client_id={clientId}&pickup={pickupJson}",
                                         "apps": {"ios": None, "android": None}},
                             "credentials": {"clientId": "••••1a2b"}, "order": 2}]}})
        assert r.status_code == 200
        assert rt.city.on_demand_provider("uber").credentials["clientId"] == "real-client-id-1a2b"


# ------------------------------------------------------------------ endpoints
async def test_providers_estimate_handoff_endpoints(bogota: City):
    app, rt = _app(bogota)
    fake = FakeOtp()
    rt._car_router = CarRouter(fake)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/ondemand/providers")
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=300"
        body = r.json()
        assert [p["id"] for p in body["providers"]] == ["taxi", "uber", "cabify", "didi", "indrive"]
        assert "credentials" not in r.text and body["tariffs"][0]["flagFall"] == 4500
        assert body["policy"]["maxFeederKm"] == 8

        q = {"fromLat": 4.6766, "fromLon": -74.0483, "toLat": 4.5978, "toLon": -74.1616, "time": "2026-09-08T10:00:00"}
        r = await c.get("/v1/cities/bogota/ondemand/estimate", params=q)
        assert r.status_code == 200, r.text
        est = r.json()
        assert est["route"]["distanceMeters"] == 19762 and est["route"]["geometry"]["precision"] == 5
        taxi = est["estimates"][0]
        assert taxi["providerId"] == "taxi" and taxi["source"] == "tariff"
        assert taxi["price"]["amount"] == 36900 and taxi["price"]["min"] == 33200 and taxi["price"]["max"] == 40600
        assert taxi["handoffUrl"].startswith("http://t/v1/cities/bogota/ondemand/handoff?providerId=taxi&")
        assert est["estimates"][1]["price"] is None and est["estimates"][1]["priceLabel"] == "Precio en la app"
        # same trip again (same 5-minute bucket): served from the car-route cache
        assert fake.calls == 1
        await c.get("/v1/cities/bogota/ondemand/estimate", params=q)
        assert fake.calls == 1
        # night surcharge at 21:00 (another time bucket -> one more OTP call)
        r = await c.get("/v1/cities/bogota/ondemand/estimate", params={**q, "time": "2026-09-08T21:00:00",
                                                                        "providerId": "taxi"})
        p = r.json()["estimates"][0]["price"]
        assert p["surchargesApplied"] == ["night"] and p["amount"] == 40700
        assert fake.calls == 2
        r = await c.get("/v1/cities/bogota/ondemand/estimate", params={**q, "providerId": "nope"})
        assert r.status_code == 404 and r.json()["error"]["code"] == "PROVIDER_NOT_FOUND"

        r = await c.get("/v1/cities/bogota/ondemand/handoff", params={"providerId": "uber", **q, "platform": "web"})
        h = r.json()
        assert h["kind"] == "url" and h["missingCredentials"] == ["clientId"] and h["url"] == "https://m.uber.com/"
        r = await c.get("/v1/cities/bogota/ondemand/handoff",
                        params={"providerId": "cabify", **q, "platform": "android", "redirect": 1})
        assert r.status_code == 302 and "com.cabify.rider" in r.headers["location"]
        assert "credentials" not in r.text


async def test_disabled_module_is_404(bogota: City):
    city = effective_city(bogota, {"mobility": {"onDemand": []}})
    assert city.on_demand_enabled() is False and city.public()["features"]["onDemand"] is False
    app, _ = _app(city)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/ondemand/providers")
        assert r.status_code == 404 and r.json()["error"]["code"] == "ONDEMAND_DISABLED"


# ------------------------------------------------------------------ planner
def test_build_variables_pairs_car_modes_with_walk():
    v = build_variables(from_lat=1, from_lon=2, to_lat=3, to_lon=4, when=WHEN, arrive_by=False, transit=["BUS"],
                        street=["WALK"], wheelchair=False, num=4, locale="es", walk_reluctance=2.0,
                        access_extra=["CAR_DROP_OFF"])
    assert v["modes"]["transit"]["access"] == ["WALK", "CAR_DROP_OFF"] and v["modes"]["transit"]["egress"] == ["WALK"]
    d = build_variables(from_lat=1, from_lon=2, to_lat=3, to_lon=4, when=WHEN, arrive_by=False, transit=[],
                        street=["CAR"], wheelchair=False, num=1, locale="es", walk_reluctance=None)
    assert d["modes"] == {"direct": ["CAR"], "directOnly": True}


def test_merge_and_attach_ondemand(bogota: City):
    origin, dest = {"name": "A", "lat": 4.6845, "lon": -74.053}, {"name": "B", "lat": 4.5978, "lon": -74.1616}
    car = plan_from_otp(bogota, json.loads((FIX / "otp_plan_car.json").read_text()), origin, dest, "2.9.0")
    combos = plan_from_otp(bogota, json.loads((FIX / "otp_plan_ondemand.json").read_text()), origin, dest, "2.9.0")
    transit = plan_from_otp(bogota, json.loads((FIX / "otp_plan.json").read_text()), origin, dest, "2.9.0")
    for it in transit["itineraries"]:
        it["source"] = "primary"
    merged = merge_ondemand(list(transit["itineraries"]), [car["itineraries"], combos["itineraries"]], 3,
                            max_feeder_m=8000)
    sources = [it["source"] for it in merged]
    assert sources.count("ondemand") == 3 and "primary" in sources          # 1 direct + 2 combos, transit kept
    assert all(it["id"] == f"it-{i}" for i, it in enumerate(merged))
    direct = next(it for it in merged if all(not lg["transit"] for lg in it["legs"]))
    assert [lg["mode"] for lg in direct["legs"]] == ["CAR"]
    # a tight feeder limit removes the combos but keeps the direct ride
    tight = merge_ondemand(list(transit["itineraries"]), [car["itineraries"], combos["itineraries"]], 3,
                           max_feeder_m=500)
    assert [it["source"] for it in tight].count("ondemand") == 1
    # decoration: providers, recommended taxi, price in the fare, CAR -> CAR_ONDEMAND
    plan = {"itineraries": merged}
    attach_to_plan(bogota, plan, when=WHEN, base_url="http://t/", locale="es")
    od = direct["legs"][0]["onDemand"]
    assert [q["providerId"] for q in od["providers"]] == ["taxi", "uber", "cabify", "didi", "indrive"]
    assert od["recommendedProviderId"] == "taxi" and od["providers"][0]["price"]["amount"] > 8000
    assert od["providers"][1]["handoffUrl"].startswith("http://t/v1/cities/bogota/ondemand/handoff?providerId=uber")
    assert direct["modesUsed"] == ["CAR_ONDEMAND"]
    assert direct["fare"]["breakdown"][0]["kind"] == "ondemand"
    assert direct["fare"]["amount"] == od["providers"][0]["price"]["amount"]
    combo = next(it for it in merged if it["source"] == "ondemand" and any(lg["transit"] for lg in it["legs"]))
    kinds = [b["kind"] for b in combo["fare"]["breakdown"]]
    assert "transit" in kinds and "ondemand" in kinds and combo["modesUsed"][1] == "CAR_ONDEMAND"
    # a city without priced providers gets a note instead of a number
    city = effective_city(bogota, {"mobility": {"onDemand": [
        {"id": "app", "name": "App", "kind": "ridehail", "estimate": {"kind": "none"},
         "handoff": {"kind": "url", "web": "https://app.example/"}, "order": 1}]}})
    plan2 = {"itineraries": [json.loads(json.dumps({**direct, "legs": [{k: v for k, v in direct["legs"][0].items()
                                                                          if k != "onDemand"}]}))]}
    attach_to_plan(city, plan2, when=WHEN, base_url="http://t/")
    f = plan2["itineraries"][0]["fare"]
    assert f["breakdown"][0]["amount"] is None and f["note"] == "Precio en la app"


# ------------------------------------------------------------------ admin validation
def test_admin_validation_errors(bogota: City):
    def bad(patch, prefix):
        with pytest.raises(ApiError) as e:
            effective_city(bogota, {"mobility": patch})
        assert e.value.status == 422 and e.value.message.startswith(prefix), e.value.message

    prov = {"id": "x", "name": "X", "kind": "ridehail", "estimate": {"kind": "none"},
            "handoff": {"kind": "url", "web": "https://x.example/"}, "order": 9}
    bad({"onDemand": [{**prov, "handoff": {"kind": "template", "template": "https://x.example/no-placeholders"}}]},
        "mobility.onDemand.0.handoff.template")
    bad({"onDemand": [{**prov, "handoff": {"kind": "url", "web": "http://insecure"}}]},
        "mobility.onDemand.0.handoff.web")
    bad({"onDemand": [{**prov, "estimate": {"kind": "tariff", "tariffId": "missing"}}]},
        "mobility.onDemand.0.estimate.tariffId")
    bad({"onDemand": [prov, {**prov, "id": "y"}]}, "mobility.onDemand: order must be unique")
    bad({"onDemand": [{**prov, "color": "red"}]}, "mobility.onDemand.0.color")
    bad({"taxiTariffs": [{"id": "t", "name": "T", "flagFall": -1, "unitPrice": 1}]}, "mobility.taxiTariffs.0.flagFall")
    bad({"taxiTariffs": [{"id": "t", "name": "T", "flagFall": 1, "unitPrice": 1,
                          "surcharges": [{"id": "z", "label": "Z", "amount": 1, "when": {"zones": ["nope"]}}]}]},
        "mobility.taxiTariffs.0.surcharges.0.when.zones")
    bad({"taxiTariffs": [{"id": "t", "name": "T", "flagFall": 1, "unitPrice": 1,
                          "surcharges": [{"id": "n", "label": "N", "amount": 1, "when": {"nightFrom": "25:00"}}]}]},
        "mobility.taxiTariffs.0.surcharges.0.when.nightFrom")
    # a valid override round-trips and reaches the effective city
    city = effective_city(bogota, {"mobility": {"onDemandPolicy": {"maxFeederKm": 3}}})
    assert city.mobility.on_demand_policy.max_feeder_km == 3 and len(city.on_demand_providers()) == 5
