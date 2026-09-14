"""v1.6 park & ride: a CAR_DROP_OFF itinerary becomes "drive to this ZPP zone, walk, ride" — or is dropped."""
import copy
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from app.admin_config import effective_city
from app.cities import City
from app.geo import decode_polyline
from app.normalize import plan_from_otp
from app.ondemand import attach_to_plan
from app.openmobility import parse_curbs_document
from app.parkride import attach_park_ride, find_parking, merge_park_ride, parking_fee

FIX = Path(__file__).parent / "fixtures"
WHEN = dt.datetime(2026, 9, 8, 10, 0, tzinfo=ZoneInfo("America/Bogota"))     # a Tuesday, 10:00

# The winning ZPP tariff as PIM publishes it (already in pesos): 6.600/h the first two hours, 9.900/h after.
ZPP_POLICY = {
    "curb_policy_id": "11f15a8f-f82f-52f6-baf8-f2fd5c17edfb", "name": "Und1431 · Horario#1", "priority": 100,
    "time_spans": [{"days_of_week": ["mon", "tue", "wed", "thu", "fri"], "time_of_day_start": "06:00",
                    "time_of_day_end": "22:00"},
                   {"days_of_week": ["sat", "sun"], "time_of_day_start": "06:00", "time_of_day_end": "22:00"}],
    "rules": [{"activity": "parking", "user_classes": ["automobile"],
               "rate": [{"rate": 6600, "rate_unit": "hour", "interval_start": 0, "interval_end": 120},
                        {"rate": 9900, "rate_unit": "hour", "interval_start": 120, "interval_end": None}]},
              {"activity": "parking", "user_classes": ["motorcycle"],
               "rate": [{"rate": 5100, "rate_unit": "hour", "interval_start": 0, "interval_end": None}]}],
}


def _zone(zid: str, name: str, lat: float, lon: float, *, available: int | None = 5) -> dict:
    """A short kerb (LineString) a few metres long around a point, ZPP style."""
    z = {"curb_zone_id": zid, "name": name, "street_name": "KR 30",
         "geometry": {"type": "LineString", "coordinates": [[lon, lat], [lon + 0.0003, lat]]},
         "curb_policy_ids": [ZPP_POLICY["curb_policy_id"]], "total_spaces": 8}
    if available is not None:
        z["available_spaces"] = available
        z["available"] = available > 0
    return z


def _city(bogota: City, **park_ride) -> City:
    return effective_city(bogota, {"openMobility": {"cds": {"enabled": True},
                                                    "parkRide": {"enabled": True, **park_ride}}})


def _combos(bogota: City) -> list[dict]:
    origin, dest = {"name": "A", "lat": 4.6845, "lon": -74.053}, {"name": "B", "lat": 4.5978, "lon": -74.1616}
    return plan_from_otp(bogota, json.loads((FIX / "otp_plan_ondemand.json").read_text()), origin, dest,
                         "2.9.0")["itineraries"]


# The three combos in the fixture drop the car at: Simón Bolívar (4.65831,-74.07804), Calle 95
# (4.68326,-74.05737) and Calle 86A (4.67705,-74.06678).
NEAR_SIMON_BOLIVAR = _zone("3f5a9d84-2b1e-4c77-9c3f-8f6b1d0a55c1", "Und1191", 4.6595, -74.0790)      # ~150 m
NEAR_CALLE_95_FULL = _zone("9e2b7c14-6d3a-4f58-b1c0-2a7d9e4f0b33", "Und1199", 4.6835, -74.0570, available=0)


def test_parking_fee_walks_the_tiers():
    rules = ZPP_POLICY["rules"]
    assert parking_fee(rules, 1) == 6600
    assert parking_fee(rules, 2) == 13200
    assert parking_fee(rules, 8) == 2 * 6600 + 6 * 9900          # 72 600: two hours at one price, six at the other
    capped = [{"activity": "parking", "rate": [{"rate": 4200, "rate_unit": "hour", "maximum_fee": 8400}]}]
    assert parking_fee(capped, 8) == 8400                          # a daily cap
    assert parking_fee([{"activity": "no parking"}], 8) is None


def test_find_parking_wants_legal_and_not_full(bogota: City):
    city = _city(bogota)
    zones, policies = parse_curbs_document({"zones": [NEAR_SIMON_BOLIVAR, NEAR_CALLE_95_FULL],
                                            "policies": [ZPP_POLICY]})
    by_id = {p["curb_policy_id"]: p for p in policies}
    hit = find_parking(zones, by_id, 4.65831, -74.07804, when=WHEN, city=city, max_walk_m=600)
    assert hit and hit["zone"]["name"] == "Und1191" and 100 < hit["distanceMeters"] < 200
    assert hit["view"]["allowed"] is True and hit["view"]["priceLabel"].startswith("$ 6.600 / hora")
    # the full one is never offered, and outside the hours nothing is legal
    assert find_parking(zones, by_id, 4.68326, -74.05737, when=WHEN, city=city, max_walk_m=600) is None
    night = WHEN.replace(hour=23)
    assert find_parking(zones, by_id, 4.65831, -74.07804, when=night, city=city, max_walk_m=600) is None
    # unknown occupancy is allowed (and said so), a tighter walk radius is honoured
    unknown = parse_curbs_document({"zones": [dict(NEAR_SIMON_BOLIVAR, available_spaces=None, available=None)],
                                    "policies": [ZPP_POLICY]})[0]
    hit = find_parking(unknown, by_id, 4.65831, -74.07804, when=WHEN, city=city, max_walk_m=600)
    assert hit["view"]["availableSpaces"] is None
    assert find_parking(zones, by_id, 4.65831, -74.07804, when=WHEN, city=city, max_walk_m=50) is None


def test_attach_park_ride_rewrites_the_itinerary_and_prices_the_dwell(bogota: City):
    city = _city(bogota)
    zones, policies = parse_curbs_document({"zones": [NEAR_SIMON_BOLIVAR, NEAR_CALLE_95_FULL],
                                            "policies": [ZPP_POLICY]})
    before = _combos(bogota)
    its = attach_park_ride(city, copy.deepcopy(before), zones, policies, when=WHEN, locale="es")
    # only the combo whose drop-off has a legal zone with spaces survives
    assert [it["parking"]["name"] for it in its] == ["Und1191"]
    it = its[0]
    orig = before[0]
    assert it["source"] == "parkride" and "CAR" in it["modesUsed"] and "CAR_ONDEMAND" not in it["modesUsed"]
    modes = [lg["mode"] for lg in it["legs"]]
    assert modes == ["WALK", "CAR", "WALK", "BUS", "WALK", "BUS", "WALK"]
    car, walk = it["legs"][1], it["legs"][2]
    assert car["parkRide"] is True and car["to"]["name"] == "Und1191"
    assert walk["from"]["name"] == "Und1191" and walk["to"]["stopId"] == "bogota:52438"
    assert walk["durationSeconds"] > 0 and walk["endTime"] == orig["legs"][2]["endTime"]   # same bus is caught
    assert car["endTime"] == walk["startTime"]
    pts = decode_polyline(walk["geometry"]["encoded"])
    assert len(pts) == 2 and abs(pts[-1][1] - 4.65831) < 1e-4
    # a longer walk than OTP's 60 s means leaving earlier by the difference
    delta = walk["durationSeconds"] - orig["legs"][2]["durationSeconds"]
    assert delta > 0
    assert it["durationSeconds"] == orig["durationSeconds"] + delta
    assert (dt.datetime.fromisoformat(it["startTime"])
            == dt.datetime.fromisoformat(orig["startTime"]) - dt.timedelta(seconds=delta))
    # the transit legs after the car are untouched
    assert it["legs"][3] == orig["legs"][3]
    p = it["parking"]
    assert p["curbZoneId"] == NEAR_SIMON_BOLIVAR["curb_zone_id"] and p["availableSpaces"] == 5 and p["totalSpaces"] == 8
    assert p["fee"] == {"amount": 72600, "currency": "COP", "dwellHours": 8.0, "estimated": True}
    assert p["allowedUntil"].startswith("2026-09-08T22:00:00") and p["walkSeconds"] == walk["durationSeconds"]
    # the fare says so, in pesos, next to the bus fares
    kinds = [b["kind"] for b in it["fare"]["breakdown"]]
    assert kinds.count("parking") == 1 and "transit" in kinds
    parking_line = next(b for b in it["fare"]["breakdown"] if b["kind"] == "parking")
    assert parking_line["label"] == "Parqueo · Und1191 (8 h)" and parking_line["amount"] == 72600
    assert it["fare"]["amount"] == 72600 + sum(b["amount"] for b in it["fare"]["breakdown"] if b["kind"] == "transit")
    # your own car gets no taxi quotes
    attach_to_plan(city, {"itineraries": its}, when=WHEN, base_url="http://t/", locale="es")
    assert "onDemand" not in car and "CAR_ONDEMAND" not in it["modesUsed"]


def test_attach_park_ride_respects_the_drive_limit_and_needs_transit(bogota: City):
    zones, policies = parse_curbs_document({"zones": [NEAR_SIMON_BOLIVAR], "policies": [ZPP_POLICY]})
    short = _city(bogota, maxDriveKm=2.0)          # Simón Bolívar is a 4.3 km drive in the fixture
    assert attach_park_ride(short, _combos(bogota), zones, policies, when=WHEN) == []
    origin, dest = {"name": "A", "lat": 4.6845, "lon": -74.053}, {"name": "B", "lat": 4.5978, "lon": -74.1616}
    direct = plan_from_otp(bogota, json.loads((FIX / "otp_plan_car.json").read_text()), origin, dest, "2.9.0")
    assert attach_park_ride(_city(bogota), direct["itineraries"], zones, policies, when=WHEN) == []


def test_merge_park_ride_never_displaces_the_best_transit(bogota: City):
    origin, dest = {"name": "A", "lat": 4.6845, "lon": -74.053}, {"name": "B", "lat": 4.5978, "lon": -74.1616}
    transit = plan_from_otp(bogota, json.loads((FIX / "otp_plan.json").read_text()), origin, dest,
                            "2.9.0")["itineraries"]
    for it in transit:
        it["source"] = "primary"
    best = transit[0]["legs"]
    # three park & ride candidates, shortest first; only two may join
    park = [dict(copy.deepcopy(transit[0]), source="parkride", durationSeconds=100 + i) for i in range(3)]
    merged = merge_park_ride(list(transit), park, 3)
    assert [it["source"] for it in merged].count("parkride") == 2      # max_combos
    assert any(it["legs"] == best and it["source"] == "primary" for it in merged)
    assert [it["id"] for it in merged] == [f"it-{i}" for i in range(len(merged))]
    assert len(merged) <= 3 + 3
