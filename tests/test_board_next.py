"""Arrival board grouping and the next-buses merge (live vs scheduled)."""
import datetime as dt

from app.geo import along_track, encode_polyline
from app.routers.board import group_board, locate_vehicle, next_rows

NOW = dt.datetime(2026, 9, 4, 15, 0, tzinfo=dt.UTC)
NOW_TS = NOW.timestamp()


def _t(minutes: float) -> str:
    return (NOW + dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _dep(route: str, minutes: float, trip: str, rt: bool = False, headsign: str | None = "Portal Sur") -> dict:
    return {"route": {"id": f"bogota:{route}", "shortName": route}, "headsign": headsign, "tripId": f"bogota:{trip}",
            "scheduledTime": _t(minutes), "realtimeTime": _t(minutes - 1) if rt else None, "realtime": rt,
            "delaySeconds": -60 if rt else None, "vehicleId": "V1" if rt else None}


def test_board_groups_by_route_and_sorts_by_first_minutes():
    deps = [_dep("B13", 12, "t1"), _dep("G12", 5, "t2", rt=True), _dep("G12", 9, "t3"), _dep("G12", 15, "t4"),
            _dep("G12", 25, "t5"), _dep("B13", 30, "t6", headsign="Norte")]
    rows = group_board(deps, per_route=3, now_ts=NOW_TS)
    assert [r["route"]["shortName"] for r in rows] == ["G12", "B13", "B13"]
    g12 = rows[0]
    assert [n["minutes"] for n in g12["next"]] == [4, 9, 15]          # capped at perRoute, realtime time wins
    assert g12["next"][0] == {"time": _t(4), "minutes": 4, "realtime": True, "delaySeconds": -60,
                              "tripId": "bogota:t2", "vehicleId": "V1"}
    assert rows[1]["headsign"] == "Portal Sur" and rows[2]["headsign"] == "Norte"


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
