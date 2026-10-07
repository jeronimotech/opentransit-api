"""The offline timetable bundle: what a rider gets with no network.

The API cannot serve this from its own tables. `stop_times.txt` is streamed once at ingest and never
stored, so departures come from OTP per stop, over the network — precisely what is missing
underground. So the bundle is built from the GTFS zip alongside the graph and published as a release
asset, the way `trip-ids.txt.gz` already is.

Two bugs these tests exist for, both found by building against the real feeds rather than by
reading the spec:

- Four of nine feeds use `frequencies.txt`. Casablanca's every trip is frequency-based, so reading
  `stop_times` alone gave it 906 departures instead of 97 599 — and Casablanca publishes no realtime
  at all, making it the city that needs offline most.
- Roma and Lisboa ship no `calendar.txt`. Iterating it alone left their service lists empty, which
  is a bundle where nothing ever runs: megabytes of departures no board would show.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from pathlib import Path

import pytest

from scripts.build_offline_bundle import NotATimetable, active_services, build


def _zip(tmp: Path, **tables: list[dict]) -> Path:
    """A GTFS zip from dict rows, so each test states only the columns it cares about."""
    p = tmp / "feed.zip"
    with zipfile.ZipFile(p, "w") as z:
        for name, rows in tables.items():
            if not rows:
                continue
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
            z.writestr(f"{name}.txt", buf.getvalue())
    return p


BASE = {
    "routes": [{"route_id": "R1", "route_short_name": "G12", "route_long_name": "Norte",
                "route_type": "3", "route_color": "D32F2F", "route_text_color": "FFFFFF"}],
    "stops": [{"stop_id": "S1", "stop_name": "Portal Sur", "stop_lat": "4.5955555",
               "stop_lon": "-74.1711111", "location_type": "0"},
              {"stop_id": "S2", "stop_name": "Av Jiménez", "stop_lat": "4.6012", "stop_lon": "-74.0718",
               "location_type": "0"}],
}


def _times(trip: str, pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"trip_id": trip, "stop_id": s, "stop_sequence": str(i + 1), "departure_time": t,
             "arrival_time": t} for i, (s, t) in enumerate(pairs)]


def test_a_plain_timetable_becomes_delta_encoded_departures(tmp_path):
    z = _zip(tmp_path, **BASE,
             trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "WK", "trip_headsign": "Norte"},
                    {"trip_id": "T2", "route_id": "R1", "service_id": "WK", "trip_headsign": "Norte"}],
             calendar=[{"service_id": "WK", "monday": "1", "tuesday": "1", "wednesday": "1",
                        "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
                        "start_date": "20260101", "end_date": "20261231"}],
             stop_times=_times("T1", [("S1", "06:00:00"), ("S2", "06:20:00")])
                        + _times("T2", [("S1", "06:10:00"), ("S2", "06:32:00")]))
    d = build(z, "testville")

    # Stop S1 has one group (route, headsign, service) holding both departures, as first + gap.
    s1 = d["stops"].index(next(s for s in d["stops"] if s["id"] == "S1"))
    groups = d["boards"][str(s1)]
    assert len(groups) == 1
    _route, _head, _svc, deltas = groups[0]
    assert deltas == [360, 10]          # 06:00, then ten minutes later
    assert d["stats"]["departures"] == 4

    # Coordinates are rounded to about a metre; seven decimals is noise that costs more than the
    # whole headsign table.
    assert d["stops"][s1]["lat"] == 4.59556


def test_a_frequency_trip_is_expanded_not_taken_literally(tmp_path):
    """Casablanca's whole timetable is frequencies, and reading stop_times alone lost 99 % of it."""
    z = _zip(tmp_path, **BASE,
             trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "WK", "trip_headsign": "Norte"}],
             calendar=[{"service_id": "WK", "monday": "1", "tuesday": "1", "wednesday": "1",
                        "thursday": "1", "friday": "1", "saturday": "1", "sunday": "1",
                        "start_date": "20260101", "end_date": "20261231"}],
             # The template starts at 06:00 and takes 20 minutes to reach S2.
             stop_times=_times("T1", [("S1", "06:00:00"), ("S2", "06:20:00")]),
             frequencies=[{"trip_id": "T1", "start_time": "07:00:00", "end_time": "08:00:00",
                           "headway_secs": "900"}])
    d = build(z, "testville")

    s1 = d["stops"].index(next(s for s in d["stops"] if s["id"] == "S1"))
    s2 = d["stops"].index(next(s for s in d["stops"] if s["id"] == "S2"))
    first_s1, *gaps_s1 = d["boards"][str(s1)][0][3]
    # Four runs: 07:00, 07:15, 07:30, 07:45. The window end is exclusive, as GTFS says.
    assert first_s1 == 7 * 60 and gaps_s1 == [15, 15, 15]

    # The second stop keeps its twenty-minute offset from the trip's own first departure rather
    # than its position in the template's clock.
    first_s2, *gaps_s2 = d["boards"][str(s2)][0][3]
    assert first_s2 == 7 * 60 + 20 and gaps_s2 == [15, 15, 15]
    assert d["stats"]["frequencyTrips"] == 1


def test_a_feed_with_no_calendar_still_has_services(tmp_path):
    """Roma and Lisboa define service only through calendar_dates.txt.

    Iterating calendar.txt alone left `services` empty, and an empty service list is a bundle where
    nothing ever runs — Roma's five million departures would never have reached a board."""
    z = _zip(tmp_path, **BASE,
             trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "D20261007",
                     "trip_headsign": "Norte"}],
             stop_times=_times("T1", [("S1", "06:00:00"), ("S2", "06:20:00")]),
             calendar_dates=[{"service_id": "D20261007", "date": "20261007", "exception_type": "1"}])
    d = build(z, "testville")

    assert [s["id"] for s in d["services"]] == ["D20261007"]
    assert d["services"][0]["from"] is None          # no calendar row, and that is valid GTFS
    assert d["stats"]["servicesWithoutCalendar"] == 1
    # It runs on exactly the date that added it, and on no other.
    assert active_services(d["services"], d["serviceExceptions"], dt.date(2026, 10, 7)) == {0}
    assert active_services(d["services"], d["serviceExceptions"], dt.date(2026, 10, 8)) == set()


def test_the_calendar_rule_the_client_has_to_copy():
    services = [{"id": "WK", "idx": 0, "days": [1, 1, 1, 1, 1, 0, 0],
                 "from": "20260101", "to": "20261231"}]
    wednesday, saturday = dt.date(2026, 10, 7), dt.date(2026, 10, 10)
    assert active_services(services, [], wednesday) == {0}
    assert active_services(services, [], saturday) == set()
    # An exception beats the weekday pattern in both directions.
    assert active_services(services, [[0, "20261007", 2]], wednesday) == set()
    assert active_services(services, [[0, "20261010", 1]], saturday) == {0}
    # And outside the service's own date range the pattern does not apply at all.
    assert active_services(services, [], dt.date(2025, 10, 7)) == set()


def test_a_trip_referring_to_a_missing_stop_is_counted_not_crashed_on(tmp_path):
    z = _zip(tmp_path, **BASE,
             trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "WK", "trip_headsign": "Norte"}],
             calendar=[{"service_id": "WK", "monday": "1", "tuesday": "1", "wednesday": "1",
                        "thursday": "1", "friday": "1", "saturday": "1", "sunday": "1",
                        "start_date": "20260101", "end_date": "20261231"}],
             stop_times=_times("T1", [("S1", "06:00:00"), ("GHOST", "06:10:00")])
                        + _times("T_UNKNOWN", [("S1", "07:00:00")]))
    d = build(z, "testville")
    # One row for a stop the feed never declared, one for a trip trips.txt never declared.
    assert d["stats"]["skippedStopTimeRows"] == 2
    assert d["stats"]["departures"] == 1


def test_times_past_midnight_keep_gtfs_semantics(tmp_path):
    """`25:10:00` is ten past one on the service day that began the previous morning. Folding it to
    01:10 would sort it to the front of the board and claim the night bus runs at dawn."""
    z = _zip(tmp_path, **BASE,
             trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "WK", "trip_headsign": "Norte"}],
             calendar=[{"service_id": "WK", "monday": "1", "tuesday": "1", "wednesday": "1",
                        "thursday": "1", "friday": "1", "saturday": "1", "sunday": "1",
                        "start_date": "20260101", "end_date": "20261231"}],
             stop_times=_times("T1", [("S1", "25:10:00")]))
    d = build(z, "testville")
    s1 = d["stops"].index(next(s for s in d["stops"] if s["id"] == "S1"))
    assert d["boards"][str(s1)][0][3] == [25 * 60 + 10]


def test_a_feed_without_stop_times_is_refused_rather_than_shipped_empty(tmp_path):
    """The worst available outcome is a bundle that installs, validates, and shows no departures.

    Every table reader here yields nothing for a missing file, so without this guard a feed with no
    stop_times produced a well-formed bundle with an empty board for every stop — a timetable that
    tells a rider their bus never runs."""
    z = _zip(tmp_path, **BASE, trips=[{"trip_id": "T1", "route_id": "R1", "service_id": "WK"}])
    with pytest.raises(NotATimetable):
        build(z, "testville")
