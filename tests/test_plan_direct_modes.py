"""Your own bike (or car) next to transit: OTP takes one plain street mode per direct search, and
"TRANSIT,WALK,BICYCLE" — what both apps send for the bike toggle — made the whole query fail, read back as
"no itineraries". Measured on production over six origin-destination pairs: 0 itineraries every time.
The router now runs one direct-only search per extra mode and merges the result in."""
import copy
import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admin_config import MemoryConfigStore
from app.cities import City
from app.errors import install_error_handlers
from app.normalize import plan_from_otp
from app.routers import plan
from app.routers.plan import build_variables, merge_direct
from app.rt import RTCache
from app.runtime import CityRuntime

FIX = Path(__file__).parent / "fixtures"
WHEN = dt.datetime(2026, 9, 8, 10, tzinfo=dt.UTC)


def _vars(street, transit=("BUS",)):
    return build_variables(from_lat=1, from_lon=2, to_lat=3, to_lon=4, when=WHEN, arrive_by=False,
                           transit=list(transit), street=street, wheelchair=False, num=5, locale="es",
                           walk_reluctance=2.0)


def test_one_plain_street_mode_per_direct_search():
    assert _vars(["WALK"])["modes"]["direct"] == ["WALK"]
    assert _vars(["BICYCLE"], transit=())["modes"]["direct"] == ["BICYCLE"]
    assert _vars(["WALK", "BICYCLE_RENTAL"])["modes"]["direct"] == ["WALK", "BICYCLE_RENTAL"]   # rental pairs with WALK
    with pytest.raises(ValueError):
        _vars(["WALK", "BICYCLE"])
    with pytest.raises(ValueError):
        _vars(["WALK", "CAR"], transit=())


class FakeOtp:
    """Answers the transit search with the captured plan and a direct-only search with a bike ride."""
    version = "2.9.0"

    def __init__(self):
        self.variables: list[dict] = []
        self.transit = json.loads((FIX / "otp_plan.json").read_text(encoding="utf-8"))
        bike = json.loads((FIX / "otp_plan_car.json").read_text(encoding="utf-8"))
        for e in bike["planConnection"]["edges"]:
            for lg in e["node"]["legs"]:
                lg["mode"] = "BICYCLE"
        self.bike = bike

    async def graphql(self, query, variables=None, locale=None):
        self.variables.append(variables)
        m = (variables or {}).get("modes") or {}
        if m.get("directOnly"):
            return copy.deepcopy(self.bike)
        return copy.deepcopy(self.transit)


def _app(city: City) -> tuple[FastAPI, CityRuntime, FakeOtp]:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(plan.router)
    fake = FakeOtp()
    rt = CityRuntime(city=city, rt=RTCache(city), otp=fake)  # type: ignore[arg-type]
    app.state.cities = {"bogota": rt}
    app.state.config_store = MemoryConfigStore()
    return app, rt, fake


@pytest.mark.anyio
async def test_bike_plus_transit_runs_two_searches_and_returns_both(bogota: City):
    app, rt, fake = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/plan", params={
            "fromLat": 4.6557, "fromLon": -74.0552, "toLat": 4.7255, "toLon": -74.0350,
            "modes": "TRANSIT,WALK,BICYCLE", "time": "2026-09-08T10:00:00", "fromName": "A", "toName": "B"})
    assert r.status_code == 200, r.text
    body = r.json()
    modes = [v["modes"] for v in fake.variables]
    assert modes[0]["direct"] == ["WALK"] and modes[0]["transit"]["access"] == ["WALK"]   # access on foot
    assert modes[1] == {"direct": ["BICYCLE"], "directOnly": True}                        # the bike, apart
    used = [it["modesUsed"] for it in body["itineraries"]]
    assert any("BICYCLE" in m for m in used) and any("BUS" in m for m in used)
    assert not any(w.startswith("NO_ITINERARIES") for w in body["warnings"])


@pytest.mark.anyio
async def test_walk_and_bike_without_transit_are_two_direct_searches(bogota: City):
    app, rt, fake = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/plan", params={
            "fromLat": 4.6557, "fromLon": -74.0552, "toLat": 4.7255, "toLon": -74.0350,
            "modes": "WALK,BICYCLE", "time": "2026-09-08T10:00:00", "fromName": "A", "toName": "B"})
    assert r.status_code == 200, r.text
    modes = [v["modes"] for v in fake.variables]
    assert modes == [{"direct": ["WALK"], "directOnly": True}, {"direct": ["BICYCLE"], "directOnly": True}]


def test_merge_direct_adds_the_shortest_new_ride_and_never_drops_the_best_transit(bogota: City):
    origin = {"name": "A", "lat": 4.6, "lon": -74.1}
    dest = {"name": "B", "lat": 4.7, "lon": -74.0}
    transit = plan_from_otp(bogota, json.loads((FIX / "otp_plan.json").read_text()), origin, dest, "2.9.0")["itineraries"]
    car = json.loads((FIX / "otp_plan_car.json").read_text())
    for e in car["planConnection"]["edges"]:
        for lg in e["node"]["legs"]:
            lg["mode"] = "BICYCLE"
    bike = plan_from_otp(bogota, car, origin, dest, "2.9.0")["itineraries"]
    for it in transit:
        it["source"] = "primary"
    n = len(transit)
    out = merge_direct(copy.deepcopy(transit), [copy.deepcopy(bike), copy.deepcopy(bike)], num=n)
    assert sum(1 for it in out if "BICYCLE" in it["modesUsed"]) == 1          # the same ride twice is one
    assert out[0]["id"] == "it-0" and len(out) <= n + 2
    assert transit[0]["legs"][0]["startTime"] in [it["legs"][0]["startTime"] for it in out]   # best transit kept
