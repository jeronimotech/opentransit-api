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
from app.routers.plan import best_plain_transit, build_variables, merge_direct, merge_ondemand
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
    transit = plan_from_otp(bogota, json.loads((FIX / "otp_plan.json").read_text()), origin, dest,
                            "2.9.0")["itineraries"]
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


def test_merge_direct_makes_room_even_when_every_transit_result_used_a_rental_bike():
    """A short page (3) full of rental-access itineraries lost the own-bike ride on production."""
    def it(i, source, rental):
        return {"id": f"it-{i}", "source": source, "rentalLegs": [1] if rental else [], "modesUsed": ["BUS"],
                "legs": [{"mode": "BUS", "transit": True, "startTime": f"2026-09-08T10:{i:02d}",
                          "from": {"stopId": f"s{i}"}, "to": {}}],
                "endTime": f"2026-09-08T11:{i:02d}:00", "durationSeconds": 3000 + i}
    chosen = [it(0, "primary", True), it(1, "rental", True), it(2, "primary", True), it(3, "primary", True),
              it(4, "rental", True)]
    bike = [{"id": "b",
             "legs": [{"mode": "BICYCLE", "transit": False, "startTime": "2026-09-08T10:00", "from": {}, "to": {}}],
             "endTime": "2026-09-08T10:40:00", "durationSeconds": 2400, "rentalLegs": [], "modesUsed": ["BICYCLE"]}]
    out = merge_direct(chosen, [bike], num=3)
    assert any("BICYCLE" in o["modesUsed"] for o in out) and len(out) == 4
    assert out[0]["modesUsed"] == ["BICYCLE"] and sum(1 for o in out if o["source"] == "rental") == 2


def _it(i, source, *, rental=False, mode="BUS", transit=True, end=None):
    return {"id": f"it-{i}", "source": source, "rentalLegs": [1] if rental else [], "modesUsed": [mode],
            "legs": [{"mode": mode, "transit": transit, "startTime": f"2026-09-08T10:{i:02d}",
                      "from": {"stopId": f"s{i}"}, "to": {}}],
            "endTime": end or f"2026-09-08T11:{i:02d}:00", "durationSeconds": 3000 + i}


def test_every_merge_keeps_the_best_plain_bus_option():
    """With everything switched on, the page ended with taxis, a parking zone and rental bikes and no bus."""
    chosen = [_it(0, "rental", rental=True, mode="BICYCLE_RENTAL"), _it(1, "primary"), _it(2, "primary"),
              _it(3, "primary"),
              _it(4, "rental", rental=True, mode="BICYCLE_RENTAL"), _it(5, "primary"), _it(6, "primary")]
    best = best_plain_transit(chosen)
    assert best is chosen[1]
    taxis = [[{**_it(9, "x", mode="CAR_ONDEMAND", transit=False, end="2026-09-08T10:30:00"),
               "legs": [{"mode": "CAR_ONDEMAND", "transit": False, "onDemand": True, "distanceMeters": 5000,
                         "startTime": "2026-09-08T10:00", "from": {}, "to": {}}]}]]
    out = merge_ondemand(chosen, taxis, 3, max_feeder_m=8000)
    assert best in out
    bike = [_it(8, "x", mode="BICYCLE", transit=False, end="2026-09-08T10:40:00")]
    out = merge_direct(out, [bike], 3)
    assert best in out and any(o["modesUsed"] == ["BICYCLE"] for o in out)


def test_a_requested_direct_ride_is_added_even_when_nothing_can_be_dropped():
    chosen = [_it(0, "primary"), _it(1, "rental", rental=True), _it(2, "ondemand", mode="CAR_ONDEMAND"),
              _it(3, "parkride", mode="CAR")]
    bike = [_it(8, "x", mode="BICYCLE", transit=False, end="2026-09-08T10:40:00")]
    out = merge_direct(chosen, [bike], 3)
    assert len(out) == 5 and any(o["modesUsed"] == ["BICYCLE"] for o in out)


@pytest.mark.anyio
async def test_with_transit_the_primary_search_walks_and_the_rental_bike_is_a_companion(bogota: City):
    """Seen on production for Kennedy → Chicó with the shared-bike toggle on: seven rows, every one with a
    rented bike to the station, no plain bus option for a rider without the app."""
    app, rt, fake = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/plan", params={
            "fromLat": 4.63, "fromLon": -74.153, "toLat": 4.672, "toLon": -74.05,
            "modes": "TRANSIT,WALK,BICYCLE_RENTAL", "time": "2026-09-08T10:00:00", "fromName": "A", "toName": "B"})
    assert r.status_code == 200, r.text
    modes = [v["modes"] for v in fake.variables]
    assert modes[0]["direct"] == ["WALK"] and modes[0]["transit"]["access"] == ["WALK"]
    assert [m["direct"] for m in modes[1:]] == [["WALK", "BICYCLE_RENTAL"], ["WALK", "BICYCLE_RENTAL"]]
    # without transit the rental mode is the direct search itself
    fake.variables.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/v1/cities/bogota/plan", params={
            "fromLat": 4.63, "fromLon": -74.153, "toLat": 4.672, "toLon": -74.05,
            "modes": "BICYCLE_RENTAL", "time": "2026-09-08T10:00:00", "fromName": "A", "toName": "B"})
    assert [v["modes"] for v in fake.variables] == [{"direct": ["WALK", "BICYCLE_RENTAL"], "directOnly": True}]
