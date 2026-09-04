import time

from google.transit import gtfs_realtime_pb2 as gtfsrt

from app.rt import RTCache, parse_alerts, parse_positions, parse_trip_updates


def _positions(ts: int) -> gtfsrt.FeedMessage:
    m = gtfsrt.FeedMessage()
    m.header.gtfs_realtime_version = "2.0"
    m.header.timestamp = ts
    for vid, tid, rid, lat, lon in (("V1", "T1", "R1", 4.65, -74.08), ("V2", "TX", "R2", 4.70, -74.10)):
        e = m.entity.add()
        e.id = vid
        v = e.vehicle
        v.vehicle.id = vid
        v.vehicle.label = f"label-{vid}"
        v.trip.trip_id = tid
        v.trip.route_id = rid
        v.position.latitude, v.position.longitude, v.position.bearing = lat, lon, 90.0
        v.timestamp = ts - 10
        v.stop_id = "S9"
        v.current_stop_sequence = 4
        v.occupancy_status = gtfsrt.VehiclePosition.MANY_SEATS_AVAILABLE
    return m


def _trip_updates(ts: int) -> gtfsrt.FeedMessage:
    m = gtfsrt.FeedMessage()
    m.header.gtfs_realtime_version = "2.0"
    e = m.entity.add()
    e.id = "tu1"
    tu = e.trip_update
    tu.trip.trip_id = "T1"
    tu.delay = 90
    su = tu.stop_time_update.add()
    su.stop_id = "S10"
    su.stop_sequence = 5
    su.arrival.time = ts + 120
    return m


def _alerts() -> gtfsrt.FeedMessage:
    m = gtfsrt.FeedMessage()
    m.header.gtfs_realtime_version = "2.0"
    e = m.entity.add()
    e.id = "A1"
    a = e.alert
    a.cause = gtfsrt.Alert.CONSTRUCTION
    a.effect = gtfsrt.Alert.DETOUR
    a.header_text.translation.add(text="Desvío en la 26", language="es")
    a.informed_entity.add(route_id="R1")
    a.informed_entity.add(stop_id="S9")
    p = a.active_period.add()
    p.start = int(time.time()) - 100
    return m


def test_parse_positions_resolves_trips_against_static():
    ents, ages, unresolved = parse_positions(_positions(1_700_000_000), known_trips={"T1"})
    assert len(ents) == 2 and unresolved == 1
    v1 = next(e for e in ents if e["id"] == "V1")
    assert v1["tripResolved"] is True and v1["routeId"] == "R1" and v1["bearing"] == 90.0
    assert v1["occupancy"] == "MANY_SEATS_AVAILABLE" and v1["stopId"] == "S9" and v1["stopSequence"] == 4
    assert ages == [1_699_999_990, 1_699_999_990]
    # without a static feed nothing is flagged unresolved
    _, _, unresolved = parse_positions(_positions(100), known_trips=None)
    assert unresolved == 0


def test_parse_trip_updates_only_first_stop():
    delays, nxt = parse_trip_updates(_trip_updates(1_700_000_000))
    assert delays == {"T1": 90}
    assert nxt == {"T1": {"stop": "S10", "seq": 5, "eta": 1_700_000_120}}


def test_parse_alerts_indexes():
    alerts, by_route, by_stop = parse_alerts(_alerts())
    assert alerts[0]["cause"] == "CONSTRUCTION" and alerts[0]["effect"] == "DETOUR"
    assert alerts[0]["header"] == "Desvío en la 26" and alerts[0]["routeIds"] == ["R1"]
    assert by_route == {"R1": [0]} and by_stop == {"S9": [0]}


def test_cache_apply_builds_frames_and_deltas(bogota):
    cache = RTCache(bogota)
    cache.set_static({"R1": {"route_id": "R1", "short_name": "B12", "component": "trunk", "agency_id": "1"}},
                     {"T1"}, {"T1": "Portal Sur"})
    now = int(time.time())
    cache.apply(_positions(now), _trip_updates(now), _alerts())
    snap = cache.snapshot()
    assert snap["type"] == "full" and snap["count"] == 2 and snap["seq"] == 1
    v1 = next(v for v in snap["vehicles"] if v["id"] == "V1")
    assert v1["routeId"] == "bogota:R1" and v1["routeShortName"] == "B12" and v1["component"] == "trunk"
    assert v1["stopId"] == "bogota:S9" and v1["timestamp"].endswith("Z")
    assert snap["health"]["pctTripResolved"] == 50.0
    assert cache.delta_frame() is None
    # second frame: V1 moved, V2 vanished
    m2 = _positions(now + 15)
    m2.entity[0].vehicle.position.latitude = 4.66
    del m2.entity[1]
    cache.apply(m2, None, None)
    d = cache.delta_frame()
    assert d["type"] == "delta" and [v["id"] for v in d["updated"]] == ["V1"] and d["removed"] == ["V2"]
    assert len(cache.history["V1"]) == 2
    assert cache.alerts_for("R1", [])[0]["id"] == "A1"
    assert cache.alerts_for(None, ["S9"])[0]["id"] == "A1"
    assert cache.alerts_for("R2", ["S1"]) == []
