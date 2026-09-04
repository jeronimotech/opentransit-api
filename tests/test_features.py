"""v1.1 derived data: fares, service windows, severity, accessibility."""
import datetime as dt
from zoneinfo import ZoneInfo

from app.cities import City
from app.features import (
    ServiceIndex,
    accessibility_block,
    accessibility_unverified,
    estimate_fare,
    hms_to_seconds,
    infer_severity,
)

TZ = ZoneInfo("America/Bogota")


def _leg(start: str, short: str | None = "G12", transit: bool = True) -> dict:
    return {"transit": transit, "startTime": start, "route": {"shortName": short} if transit else None}


def test_fare_single_boarding(bogota: City):
    f = estimate_fare(bogota, [_leg("2026-09-04T08:00:00-05:00", None, False), _leg("2026-09-04T08:10:00-05:00")])
    assert f["amount"] == 3200 and f["currency"] == "COP" and f["estimated"] is True
    assert f["breakdown"] == [{"label": "Pasaje", "amount": 3200, "route": "G12"}]


def test_fare_walk_only_is_zero(bogota: City):
    f = estimate_fare(bogota, [_leg("2026-09-04T08:00:00-05:00", None, False)])
    assert f["amount"] == 0 and f["breakdown"] == []


def test_fare_one_transfer_inside_window(bogota: City):
    f = estimate_fare(bogota, [_leg("2026-09-04T08:00:00-05:00"), _leg("2026-09-04T08:40:00-05:00", "DH209")])
    assert f["amount"] == 3200
    assert [b["label"] for b in f["breakdown"]] == ["Pasaje", "Transbordo"]


def test_fare_three_boardings_exceed_max_transfers(bogota: City):
    legs = [_leg("2026-09-04T08:00:00-05:00"), _leg("2026-09-04T08:20:00-05:00", "A"),
            _leg("2026-09-04T08:40:00-05:00", "B"), _leg("2026-09-04T09:00:00-05:00", "C")]
    f = estimate_fare(bogota, legs)
    # base + 2 free transfers + a new base for the 4th boarding (max_transfers = 2)
    assert f["amount"] == 6400
    assert [b["label"] for b in f["breakdown"]] == ["Pasaje", "Transbordo", "Transbordo", "Pasaje"]


def test_fare_window_exceeded_pays_again(bogota: City):
    f = estimate_fare(bogota, [_leg("2026-09-04T08:00:00-05:00"), _leg("2026-09-04T10:30:00-05:00", "B")])
    assert f["amount"] == 6400 and f["breakdown"][1]["label"] == "Pasaje"


def test_fare_english_labels_and_no_config(bogota: City):
    f = estimate_fare(bogota, [_leg("2026-09-04T08:00:00-05:00")], "en")
    assert f["breakdown"][0]["label"] == "Fare"
    nofare = bogota.model_copy(update={"fares": None})
    assert estimate_fare(nofare, [_leg("2026-09-04T08:00:00-05:00")]) is None


# ---------------------------------------------------------------- service windows

def _index() -> ServiceIndex:
    idx = ServiceIndex()
    # service 1 = weekdays, 2 = saturday, 4 = sunday; windows in seconds
    idx.calendar = {
        "1": ((1, 1, 1, 1, 1, 0, 0), dt.date(2026, 1, 1), dt.date(2026, 12, 31)),
        "2": ((0, 0, 0, 0, 0, 1, 0), dt.date(2026, 1, 1), dt.date(2026, 12, 31)),
        "4": ((0, 0, 0, 0, 0, 0, 1), dt.date(2026, 1, 1), dt.date(2026, 12, 31)),
    }
    idx.windows = {"G12": [("1", 4 * 3600, 23 * 3600), ("2", 5 * 3600, 26 * 3600 + 27 * 60)],
                   "NIGHT": [("1", 22 * 3600, 25 * 3600)]}
    # 2026-09-07 is a Monday holiday: weekday service removed, sunday service added
    idx.exceptions = {("1", dt.date(2026, 9, 7)): 2, ("4", dt.date(2026, 9, 7)): 1}
    return idx


def test_window_active_on_weekday():
    w = _index().window_for("G12", dt.datetime(2026, 9, 4, 10, 0, tzinfo=TZ))   # Friday
    assert w == {"start": "04:00", "end": "23:00", "endsNextDay": False, "active": True, "nextStart": None,
                 "nextStartDay": None, "hasServiceToday": True, "source": "gtfs"}


def test_window_out_of_hours_gives_next_start_today():
    w = _index().window_for("G12", dt.datetime(2026, 9, 4, 3, 30, tzinfo=TZ))
    assert w["active"] is False and w["nextStart"] == "04:00" and w["nextStartDay"] == "today"


def test_window_after_last_departure_points_to_tomorrow():
    w = _index().window_for("G12", dt.datetime(2026, 9, 4, 23, 30, tzinfo=TZ))   # Friday night -> Saturday 05:00
    assert w["active"] is False and w["nextStart"] == "05:00" and w["nextStartDay"] == "tomorrow"


def test_window_crossing_midnight_is_still_active():
    # Saturday service runs until 26:27 -> 02:27 Sunday; at 01:00 Sunday it is still active
    w = _index().window_for("G12", dt.datetime(2026, 9, 6, 1, 0, tzinfo=TZ))     # Sunday
    assert w["active"] is True
    w2 = _index().window_for("G12", dt.datetime(2026, 9, 5, 20, 0, tzinfo=TZ))  # Saturday evening
    assert w2["end"] == "02:27" and w2["endsNextDay"] is True


def test_window_calendar_dates_exception_holiday():
    holiday = dt.datetime(2026, 9, 7, 10, 0, tzinfo=TZ)   # Monday, weekday service removed, sunday added
    assert "1" not in _index().active_services(holiday.date())
    assert "4" in _index().active_services(holiday.date())
    w = _index().window_for("NIGHT", holiday)        # NIGHT only runs on service 1
    assert w["active"] is False and w["hasServiceToday"] is False and w["start"] is None
    assert w["nextStart"] == "22:00" and w["nextStartDay"] == "tomorrow"


def test_window_unknown_route():
    assert _index().window_for("NOPE", dt.datetime(2026, 9, 4, 10, 0, tzinfo=TZ)) is None


def test_hms_parsing():
    assert hms_to_seconds("26:27:26") == 95246
    assert hms_to_seconds("4:05:00") == 14700
    assert hms_to_seconds("bad") is None


# ---------------------------------------------------------------- severity / accessibility

def test_severity_inference():
    assert infer_severity("SEVERE", "DETOUR") == "SEVERE"           # feed wins
    assert infer_severity(None, "NO_SERVICE") == "SEVERE"
    assert infer_severity("UNKNOWN_SEVERITY", "DETOUR") == "WARNING"
    assert infer_severity(None, "STOP_MOVED") == "WARNING"
    assert infer_severity(None, "OTHER_EFFECT") == "INFO"
    assert infer_severity(None, None) == "INFO"


def test_accessibility_constant_value_is_unverified():
    assert accessibility_unverified({1: 8335}) is True
    assert accessibility_unverified({1: 990, 2: 10}) is True
    assert accessibility_unverified({1: 500, 2: 500}) is False
    assert accessibility_unverified({0: 8335}) is False          # "no information" is honest
    assert accessibility_unverified({}) is False
    blk = accessibility_block("accessible", True)
    assert blk["verified"] is False and blk["source"] == "gtfs" and "no verificado" in blk["note"]
    assert accessibility_block("accessible", False)["verified"] is True
    assert accessibility_block("unknown", True) == {"wheelchair": "unknown", "source": "none", "verified": False,
                                                    "note": None}
