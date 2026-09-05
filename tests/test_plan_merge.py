"""Rental-aware planning: one OTP query per rental mode, a rental-biased companion search, merge + guarantees,
and dropping rental modes that no network can serve right now."""
import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from app.cities import BikeShareNetwork, City
from app.gbfs import GbfsNetwork
from app.normalize import plan_from_otp
from app.routers.plan import RENTAL_BIAS, build_variables, merge_plans, rental_availability, resolve_rental_modes

FIX = Path(__file__).parent / "fixtures"
ORIGIN = {"name": "A", "lat": 1, "lon": 2}
DEST = {"name": "B", "lat": 3, "lon": 4}


def _its(bogota: City, name: str) -> list[dict]:
    data = json.loads((FIX / name).read_text(encoding="utf-8"))
    return plan_from_otp(bogota, data, ORIGIN, DEST, "2.9.0")["itineraries"]


def _shift(it: dict, minutes: int, duration: int | None = None) -> dict:
    """A distinct copy of an itinerary: later start/end (and legs) so its signature differs."""
    out = copy.deepcopy(it)

    def bump(ts: str) -> str:
        t = dt.datetime.fromisoformat(ts) + dt.timedelta(minutes=minutes)
        return t.isoformat()
    out["startTime"], out["endTime"] = bump(out["startTime"]), bump(out["endTime"])
    for lg in out["legs"]:
        lg["startTime"], lg["endTime"] = bump(lg["startTime"]), bump(lg["endTime"])
    if duration is not None:
        out["durationSeconds"] = duration
    return out


# ------------------------------------------------------------------ query building
def _vars(street, **kw):
    return build_variables(from_lat=1, from_lon=2, to_lat=3, to_lon=4, when=dt.datetime(2026, 9, 4, 8, tzinfo=dt.UTC),
                           arrive_by=False, transit=["BUS"], street=street, wheelchair=False, num=3, locale="es",
                           walk_reluctance=2.0, **kw)


def test_one_rental_mode_per_query_and_bias_preferences():
    with pytest.raises(ValueError):
        _vars(["WALK", "BICYCLE_RENTAL", "SCOOTER_RENTAL"])
    v = _vars(["WALK", "SCOOTER_RENTAL"], bicycle_reluctance=RENTAL_BIAS["bicycle_reluctance"])
    assert v["modes"]["transit"]["access"] == ["WALK", "SCOOTER_RENTAL"]
    assert v["preferences"]["street"] == {"walk": {"reluctance": 2.0}, "bicycle": {"reluctance": 1.0}}


# ------------------------------------------------------------------ availability
def test_resolve_rental_modes_drops_unavailable_with_warning():
    street, warn = resolve_rental_modes(["WALK", "BICYCLE_RENTAL", "SCOOTER_RENTAL"],
                                        {"BICYCLE_RENTAL": True, "SCOOTER_RENTAL": False})
    assert street == ["WALK", "BICYCLE_RENTAL"] and warn == ["MODE_NO_VEHICLES: SCOOTER_RENTAL"]
    # unknown (status not loaded yet) keeps the mode; nothing left -> WALK
    assert resolve_rental_modes(["SCOOTER_RENTAL"], {"SCOOTER_RENTAL": None}) == (["SCOOTER_RENTAL"], [])
    assert resolve_rental_modes(["SCOOTER_RENTAL"], {"SCOOTER_RENTAL": False})[0] == ["WALK"]


class _Rt:
    def __init__(self, gbfs):
        self.gbfs = gbfs


def _net(nid: str, factors: list[str], status: dict | None, types: dict) -> GbfsNetwork:
    g = GbfsNetwork("t", BikeShareNetwork(id=nid, name=nid, network=nid, gbfs_url="https://x/gbfs.json",
                                          form_factors=factors))
    g.vehicle_types = types
    g.station_status = status or {}
    return g


TYPES = {"FIT": {"vehicle_type_id": "FIT", "form_factor": "bicycle"},
         "CHLOE": {"vehicle_type_id": "CHLOE", "form_factor": "scooter_standing"}}


def test_rental_availability_from_station_status():
    bikes_only = {"1": {"is_renting": True, "vehicle_types_available": [{"vehicle_type_id": "FIT", "count": 3},
                                                                        {"vehicle_type_id": "CHLOE", "count": 0}]}}
    rt = _Rt({"a": _net("a", ["bicycle", "scooter"], bikes_only, TYPES)})
    assert rental_availability(rt) == {"BICYCLE_RENTAL": True, "SCOOTER_RENTAL": False}
    # status not loaded yet -> unknown for configured families, False for families nobody configures
    rt = _Rt({"a": _net("a", ["bicycle"], None, TYPES)})
    assert rental_availability(rt) == {"BICYCLE_RENTAL": None, "SCOOTER_RENTAL": False}
    # a second network that stocks scooters makes the mode available
    scooters = {"9": {"is_renting": True, "vehicle_types_available": [{"vehicle_type_id": "CHLOE", "count": 2}]}}
    rt = _Rt({"a": _net("a", ["bicycle"], bikes_only, TYPES), "z": _net("z", ["scooter"], scooters, TYPES)})
    assert rental_availability(rt) == {"BICYCLE_RENTAL": True, "SCOOTER_RENTAL": True}
    assert rt.gbfs["z"].form_factors() == ["scooter"]


# ------------------------------------------------------------------ merge
def test_merge_adds_rental_from_companion_and_sorts_by_arrival(bogota: City):
    primary = _its(bogota, "otp_plan.json")            # 08:02 -> 08:47, WALK+BUS
    rental = _its(bogota, "otp_plan_rental.json")      # 16:53 -> 17:06, WALK+BICYCLE(rental)
    out = merge_plans(primary, [rental], 5)
    assert [it["source"] for it in out] == ["primary", "rental"]
    assert [it["id"] for it in out] == ["it-0", "it-1"]
    assert out[0]["endTime"] < out[1]["endTime"] and out[1]["rentalLegs"] == 1


def test_merge_dedupes_identical_itineraries(bogota: City):
    rental = _its(bogota, "otp_plan_rental.json")
    out = merge_plans(rental, [rental, copy.deepcopy(rental)], 5)
    assert len(out) == 1 and out[0]["source"] == "primary"


def test_merge_guarantees_two_rental_options_and_caps_total(bogota: City):
    base = _its(bogota, "otp_plan.json")[0]
    primary = [_shift(base, i * 10) for i in range(5)]          # 5 walking+bus results
    r = _its(bogota, "otp_plan_rental.json")[0]
    rentals = [_shift(r, 0, 900), _shift(r, 5, 700), _shift(r, 10, 1200)]
    out = merge_plans(primary, [rentals], 5)
    assert len(out) == 7                                        # cap = num + 2
    chosen = [it for it in out if it["source"] == "rental"]
    assert len(chosen) == 2 and sorted(it["durationSeconds"] for it in chosen) == [700, 900]
    # when the primary list already fills the cap, the worst non-rental result makes room for the guarantee
    primary = [_shift(base, i * 10) for i in range(7)]
    out = merge_plans(primary, [rentals], 5)
    assert len(out) == 7 and sum(1 for it in out if it["source"] == "rental") == 2
    assert sum(1 for it in out if it["source"] == "primary") == 5
    # no rental candidates -> primary untouched
    assert [it["source"] for it in merge_plans(primary, [[]], 5)] == ["primary"] * 5
