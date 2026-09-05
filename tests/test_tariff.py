"""Taximeter tariff engine (v1.4): distance/time units, minimum fare, surcharges by time/day/zone/option, band."""
import datetime as dt
from zoneinfo import ZoneInfo

from app.cities import TaxiSurcharge, TaxiSurchargeWhen, TaxiTariff, TaxiZone
from app.tariff import applicable_surcharges, estimate, is_holiday, point_in_polygon

TZ = ZoneInfo("America/Bogota")
AIRPORT = [[-74.165, 4.684], [-74.118, 4.684], [-74.118, 4.716], [-74.165, 4.716]]


def tariff() -> TaxiTariff:
    return TaxiTariff(
        id="t", name="Tarifa", currency="COP", flag_fall=4500, unit_price=159, unit_meters=100, unit_seconds=30,
        minimum_fare=8000,
        surcharges=[
            TaxiSurcharge(id="night", label="Nocturno", amount=3800,
                          when=TaxiSurchargeWhen(night_from="19:00", night_to="06:00", sundays=True, holidays=True)),
            TaxiSurcharge(id="airport", label="Aeropuerto", amount=8000, when=TaxiSurchargeWhen(zones=["airport"])),
            TaxiSurcharge(id="door", label="Puerta a puerta", amount=1500, when=TaxiSurchargeWhen(optional=True)),
        ],
        zones=[TaxiZone(id="airport", name="El Dorado", polygon=AIRPORT)],
    )


def at(y, m, d, hh, mm=0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=TZ)


def test_daytime_ride_units_and_band():
    # 10 km, 20 min on a Monday morning: 100 distance units + 6 waiting units (15 % of 1200 s / 30 s)
    p = estimate(tariff(), 10_000, 1200, at(2026, 9, 7, 10), country="CO")
    assert p["amount"] == 4500 + 106 * 159 == 21354 or p["amount"] == 21400      # rounded to 100
    assert p["amount"] == 21400 and p["min"] == 19300 and p["max"] == 23500
    assert p["surchargesApplied"] == [] and p["breakdown"][0]["label"] == "Carrera (10 km)"
    assert p["currency"] == "COP" and p["estimated"] is True and p["tariffId"] == "t"


def test_minimum_fare_short_trip():
    p = estimate(tariff(), 800, 180, at(2026, 9, 7, 10), country="CO")
    assert p["amount"] == 8000 and p["breakdown"][0]["label"] == "Carrera mínima"


def test_night_surcharge_wraps_midnight():
    t = tariff()
    assert [s.id for s in applicable_surcharges(t, at(2026, 9, 7, 21), country="CO", points=[])] == ["night"]
    assert [s.id for s in applicable_surcharges(t, at(2026, 9, 8, 5, 30), country="CO", points=[])] == ["night"]
    assert applicable_surcharges(t, at(2026, 9, 8, 6, 0), country="CO", points=[]) == []


def test_sunday_and_colombian_holiday():
    t = tariff()
    assert [s.id for s in applicable_surcharges(t, at(2026, 9, 6, 12), country="CO", points=[])] == ["night"]  # Sunday
    assert is_holiday("CO", dt.date(2026, 1, 12))                                                     # Reyes (observed)
    assert [s.id for s in applicable_surcharges(t, at(2026, 1, 12, 12), country="CO", points=[])] == ["night"]
    assert applicable_surcharges(t, at(2026, 1, 12, 12), country=None, points=[]) == []   # no country: no holidays
    assert applicable_surcharges(t, at(2026, 1, 13, 12), country="XX", points=[]) == []               # unknown country


def test_airport_zone_and_optional_door_to_door():
    t = tariff()
    assert point_in_polygon(-74.1469, 4.7016, AIRPORT) and not point_in_polygon(-74.05, 4.65, AIRPORT)
    p = estimate(t, 17_800, 1500, at(2026, 9, 7, 10), country="CO", points=[(4.6766, -74.0483), (4.7016, -74.1469)])
    assert p["surchargesApplied"] == ["airport"] and p["breakdown"][-1] == {"label": "Aeropuerto", "amount": 8000}
    p2 = estimate(t, 17_800, 1500, at(2026, 9, 7, 10), country="CO", points=[(4.6766, -74.0483), (4.7016, -74.1469)],
                  optional_ids={"door"})
    assert p2["surchargesApplied"] == ["airport", "door"] and p2["amount"] == p["amount"] + 1500
    # night + airport stack
    p3 = estimate(t, 17_800, 1500, at(2026, 9, 7, 22), country="CO", points=[(4.7016, -74.1469)])
    assert p3["surchargesApplied"] == ["night", "airport"]


def test_english_labels():
    p = estimate(tariff(), 800, 180, at(2026, 9, 7, 10), locale="en")
    assert p["breakdown"][0]["label"] == "Minimum fare" and "meter" in p["note"]
