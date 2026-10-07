"""Arrival board grouping and the next-buses merge (live vs scheduled)."""
import datetime as dt

from app.geo import along_track, encode_polyline
from app.normalize import apply_stop_predictions, merge_departures
from app.routers.board import group_board, locate_vehicle, next_rows

NOW = dt.datetime(2026, 9, 4, 15, 0, tzinfo=dt.UTC)
NOW_TS = NOW.timestamp()


def _t(minutes: float) -> str:
    return (NOW + dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _dep(route: str, minutes: float, trip: str, rt: bool = False, headsign: str | None = "Portal Sur") -> dict:
    return {"route": {"id": f"bogota:{route}", "shortName": route}, "headsign": headsign, "tripId": f"bogota:{trip}",
            "scheduledTime": _t(minutes), "realtimeTime": _t(minutes - 1) if rt else None, "realtime": rt,
            "delaySeconds": -60 if rt else None, "vehicleId": "V1" if rt else None,
            "realtimeSource": "trip" if rt else None}


def test_board_groups_by_route_and_sorts_by_first_minutes():
    deps = [_dep("B13", 12, "t1"), _dep("G12", 5, "t2", rt=True), _dep("G12", 9, "t3"), _dep("G12", 15, "t4"),
            _dep("G12", 25, "t5"), _dep("B13", 30, "t6", headsign="Norte")]
    rows = group_board(deps, per_route=3, now_ts=NOW_TS)
    assert [r["route"]["shortName"] for r in rows] == ["G12", "B13", "B13"]
    g12 = rows[0]
    assert [n["minutes"] for n in g12["next"]] == [4, 9, 15]          # capped at perRoute, realtime time wins
    assert g12["next"][0] == {"time": _t(4), "minutes": 4, "realtime": True, "source": "live",
                              "delaySeconds": -60, "tripId": "bogota:t2", "vehicleId": "V1",
                              "vehicle": None}
    assert rows[1]["headsign"] == "Portal Sur" and rows[2]["headsign"] == "Norte"



def test_board_carries_the_vehicle_so_the_stop_map_can_draw_it():
    """A board row hands over where the bus is, not only when it is due.

    The stop page draws approaching buses on its map; "3 min" from a feed that may be stale is
    exactly the claim a rider cannot check, and a position two blocks away is. Departures with no
    live match stay null rather than borrowing another bus's position."""
    live = {"id": "V1", "lat": 4.63, "lon": -74.08, "bearing": 12.0, "tripMatch": "id"}
    deps = [dict(_dep("G12", 5, "t2", rt=True), vehicle=live), _dep("B13", 12, "t1")]
    rows = group_board(deps, per_route=3, now_ts=NOW_TS)
    by_route = {r["route"]["shortName"]: r for r in rows}
    assert by_route["G12"]["next"][0]["vehicle"] == live
    assert by_route["B13"]["next"][0]["vehicle"] is None

def test_board_separates_a_rescued_arrival_from_a_live_one():
    """Three states, not two: `realtime` is true for both a matched trip and a rescued one.

    A prediction paired by stop and route after the trip id failed to resolve is our inference, and
    in Bogota or Roma — whose feeds rotate trip ids between publications — it is most of the board.
    Showing it as "live" claims a bus reported itself when nothing did, so the board says
    "estimated" instead and keeps "live" for the arrivals that earned it."""
    matched = _dep("G12", 5, "t2", rt=True)
    rescued = dict(_dep("B13", 7, "t3", rt=True), realtimeSource="stop")
    timetable = _dep("C15", 9, "t4")
    rows = group_board([matched, rescued, timetable], per_route=3, now_ts=NOW_TS)
    by_route = {r["route"]["shortName"]: r["next"][0] for r in rows}
    assert by_route["G12"]["source"] == "live"
    assert by_route["B13"]["source"] == "estimated"
    assert by_route["C15"]["source"] == "scheduled"
    # The rescued one is still realtime — the time is better than the timetable's, just not reported.
    assert by_route["B13"]["realtime"] is True


def test_a_bus_matched_to_its_trip_by_schedule_is_an_estimate_too():
    """The other inference, and on `/board` the only one that actually occurs.

    `apply_stop_predictions` runs on `/departures`, so `realtimeSource` here is only ever "trip".
    But the vehicle the board carries records how it was attached to that trip: `tripMatch` is
    "schedule" when the id the feed gave us was not one the graph knows and we placed the bus by
    where and when it is instead. Bogota's feed rotates ids between publications, so that is
    hundreds of buses at a time. The time is real; the pairing is ours."""
    by_id = {"id": "V1", "lat": 4.63, "lon": -74.08, "tripMatch": "id"}
    by_sched = {"id": "V2", "lat": 4.64, "lon": -74.09, "tripMatch": "schedule"}
    rows = group_board([dict(_dep("G12", 5, "t2", rt=True), vehicle=by_id),
                        dict(_dep("B13", 7, "t3", rt=True), vehicle=by_sched)],
                       per_route=3, now_ts=NOW_TS)
    by_route = {r["route"]["shortName"]: r["next"][0] for r in rows}
    assert by_route["G12"]["source"] == "live"
    assert by_route["B13"]["source"] == "estimated"


def test_a_departure_with_no_vehicle_is_still_live_when_the_trip_matched():
    """Not having a position is not a reason to doubt the time.

    OTP matched the trip and gave us a prediction; our separate vehicle frame simply has no bus on
    it this second. Downgrading that to "estimated" would under-claim on every feed that publishes
    trip updates without positions."""
    rows = group_board([_dep("G12", 5, "t2", rt=True)], per_route=3, now_ts=NOW_TS)
    assert rows[0]["next"][0]["vehicle"] is None
    assert rows[0]["next"][0]["source"] == "live"


def test_stop_paired_arrivals_reach_the_board_as_estimates_without_bare_rows():
    """What `/board` does with a feed whose trip ids are not the schedule's.

    Toronto's and Kuala Lumpur's realtime resolves by trip id almost never, so the board used to
    show plain timetable times for arrivals the API already had on `/departures`. The board now runs
    the same stop-keyed pairing, and keeps only the departures that got paired: the leftovers that
    `apply_stop_predictions` appends have no headsign and no scheduled time, and on a board grouped
    by (route, headsign) they would appear as a second half-empty row for a route already listed.
    """
    deps = [_dep("501", 10, "t1"), _dep("504", 30, "t2")]
    arrivals = [
        {"route": "501", "eta": int((NOW + dt.timedelta(minutes=8)).timestamp()), "seq": 3},
        # same route, far outside the 15-minute window of any scheduled departure -> a leftover
        {"route": "501", "eta": int((NOW + dt.timedelta(minutes=70)).timestamp()), "seq": 4},
    ]
    out = apply_stop_predictions(deps, arrivals, _City(), int(NOW_TS))
    kept = [d for d in out if d.get("scheduledTime")]
    assert len(out) == 3 and len(kept) == 2          # one leftover appended, then dropped

    rows = group_board(merge_departures(kept), per_route=3, now_ts=NOW_TS)
    by_route = {r["route"]["shortName"]: r for r in rows}
    # the paired one is realtime and honest about how it got there
    assert by_route["501"]["next"][0]["source"] == "estimated"
    assert by_route["501"]["next"][0]["realtime"] is True
    assert by_route["501"]["next"][0]["minutes"] == 8
    # the untouched one stays on the timetable
    assert by_route["504"]["next"][0]["source"] == "scheduled"
    # and no row lost its identity to a bare route ref
    assert all(r["route"].get("shortName") for r in rows)
    assert len(rows) == 2


# ---- next buses -------------------------------------------------------------

LINE = [(-74.0500, 4.7500), (-74.0500, 4.7000), (-74.0500, 4.6500)]   # straight south, ~11 km
STOPS = ["bogota:A", "bogota:B", "bogota:C"]


def _pattern() -> dict:
    along = [along_track(LINE, lon, lat)[0] for lon, lat in LINE]
    return {"code": "p1", "headsign": "Sur", "line": LINE, "stopIds": STOPS, "along": along}


class _City:
    @staticmethod
    def scoped(x):
        return x if x is None or x.startswith("bogota:") else f"bogota:{x}"

    @staticmethod
    def unscoped(x):
        return x[7:] if x and x.startswith("bogota:") else x


def test_locate_vehicle_prefers_rt_stop_id_then_projection():
    pats = [_pattern()]
    v = {"stopId": "B", "lat": 4.72, "lon": -74.0500}
    pat, idx, along = locate_vehicle(v, pats, _City())
    assert pat is pats[0] and idx == 1 and 3000 < along < 3500
    v2 = {"stopId": None, "lat": 4.68, "lon": -74.0501}      # between B and C -> next stop index 2
    _, idx2, _ = locate_vehicle(v2, pats, _City())
    assert idx2 == 2
    far = {"stopId": None, "lat": 4.68, "lon": -74.10}       # 5 km off the line -> not on this pattern
    assert locate_vehicle(far, pats, _City()) == (None, None, None)


def test_next_rows_merge_live_estimated_scheduled():
    pats = [_pattern()]
    vehicles = [
        {"id": "v-live", "routeId": "G12", "tripId": "t-live", "stopId": "A", "lat": 4.75, "lon": -74.05},
        {"id": "v-est", "routeId": "G12", "tripId": "t-est", "stopId": "B", "lat": 4.71, "lon": -74.05},
        {"id": "v-past", "routeId": "G12", "tripId": "t-past", "stopId": None, "lat": 4.64, "lon": -74.05},  # beyond C
    ]
    deps = [_dep("G12", 20, "t-live", rt=True), _dep("G12", 30, "t-sched"), _dep("G12", 45, "t-sched2")]
    rows = next_rows(vehicles, pats, {"bogota:C"}, deps, _City(), "trunk", NOW_TS, limit=3)
    srcs = {r["vehicle"]["id"] if r["vehicle"] else r["tripId"]: r["source"] for r in rows}
    assert srcs["v-est"] == "estimated" and srcs["v-live"] == "live" and srcs["bogota:t-sched"] == "scheduled"
    est = next(r for r in rows if r["source"] == "estimated")
    # the bus is 1.1 km before B: 1.1 km to B + 5.5 km B->C
    assert est["stopsAway"] == 1 and 6000 < est["distanceMeters"] < 7000 and 15 <= est["minutes"] <= 25
    live = next(r for r in rows if r["source"] == "live")
    assert live["minutes"] == 19 and live["stopsAway"] == 2 and live["delaySeconds"] == -60
    assert all(r["vehicle"] is None or r["vehicle"]["id"] != "v-past" for r in rows)
    assert [r["minutes"] for r in rows] == sorted(r["minutes"] for r in rows) and len(rows) == 3


def test_along_track_and_polyline_roundtrip():
    enc = encode_polyline(LINE)
    from app.geo import decode_polyline
    assert [(round(x, 5), round(y, 5)) for x, y in decode_polyline(enc)] == LINE
    d, off = along_track(LINE, -74.0500, 4.7000)
    assert 5400 < d < 5650 and off < 1
