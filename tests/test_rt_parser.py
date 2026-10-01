import time

from google.transit import gtfs_realtime_pb2 as gtfsrt

from app.rt import (
    BACKOFF_MAX_S,
    BACKOFF_START_S,
    TURNED_AWAY,
    FeedBackoff,
    RTCache,
    norm_gtfs_time,
    parse_alerts,
    parse_positions,
    parse_trip_updates,
)


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
    tu.trip.route_id = "R1"
    tu.delay = 90
    su = tu.stop_time_update.add()
    su.stop_id = "S10"
    su.stop_sequence = 5
    su.arrival.time = ts + 120
    # A real update carries the whole rest of the trip, not just the next stop.
    su2 = tu.stop_time_update.add()
    su2.stop_id = "S11"
    su2.stop_sequence = 6
    su2.arrival.time = ts + 300
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
    ents, ages, unresolved, rescued = parse_positions(_positions(1_700_000_000), known_trips={"T1"})
    assert len(ents) == 2 and unresolved == 1 and rescued == 0
    v1 = next(e for e in ents if e["id"] == "V1")
    assert v1["tripResolved"] is True and v1["routeId"] == "R1" and v1["bearing"] == 90.0
    assert v1["tripMatch"] == "id"
    assert v1["occupancy"] == "MANY_SEATS_AVAILABLE" and v1["stopId"] == "S9" and v1["stopSequence"] == 4
    assert ages == [1_699_999_990, 1_699_999_990]
    # the one whose id is not in the schedule says so rather than claiming a match
    v2 = next(e for e in ents if e["id"] == "V2")
    assert v2["tripResolved"] is False and v2["tripMatch"] is None
    # without a static feed nothing is flagged unresolved
    _, _, unresolved, _ = parse_positions(_positions(100), known_trips=None)
    assert unresolved == 0


def test_a_vehicle_whose_trip_id_the_feed_invented_is_matched_by_route_and_start_time():
    """TransMilenio ships every vehicle it cannot match to its own schedule as `ADDED`, with a
    trip_id that exists nowhere in its static feed — 11-12 % of the fleet, none of it a genuinely
    extra trip. Route plus start time names the trip it is really running."""
    m = _positions(1_700_000_000)
    v2 = next(e.vehicle for e in m.entity if e.id == "V2")
    v2.trip.start_time = "6:05:00"                    # the feed writes H:MM:SS, the schedule HH:MM:SS
    v2.trip.schedule_relationship = gtfsrt.TripDescriptor.ADDED

    index = {("R2", "06:05:00"): "T2"}
    ents, _, unresolved, rescued = parse_positions(m, known_trips={"T1"}, schedule_index=index)
    assert unresolved == 0 and rescued == 1
    got = next(e for e in ents if e["id"] == "V2")
    assert got["tripId"] == "T2" and got["tripResolved"] is True
    # the match is recorded as inferred, so nothing downstream claims the id matched
    assert got["tripMatch"] == "schedule"


def test_the_rescue_never_guesses():
    m = _positions(1_700_000_000)
    v2 = next(e.vehicle for e in m.entity if e.id == "V2")
    v2.trip.start_time = "06:05:00"
    # a pair that names two trips is left out of the index upstream, so it simply misses
    for index in ({}, {("R2", "07:00:00"): "T2"}, {("R9", "06:05:00"): "T2"}):
        _, _, unresolved, rescued = parse_positions(m, known_trips={"T1"}, schedule_index=index)
        assert (unresolved, rescued) == (1, 0)
    # and a vehicle with no start time at all cannot be rescued
    v2.trip.ClearField("start_time")
    _, _, unresolved, rescued = parse_positions(m, known_trips={"T1"}, schedule_index={("R2", ""): "T2"})
    assert (unresolved, rescued) == (1, 0)


def test_gtfs_times_normalise_without_touching_times_past_midnight():
    assert norm_gtfs_time("6:05:00") == "06:05:00"
    assert norm_gtfs_time("06:05:00") == "06:05:00"
    assert norm_gtfs_time(" 25:10:00 ") == "25:10:00"      # a trip that leaves after midnight
    assert norm_gtfs_time(None) == "" and norm_gtfs_time("") == ""


def test_parse_trip_updates_keeps_the_next_stop_and_every_stop():
    """`trip_next` is still the first stop only — it answers "where is this trip now".
    `by_stop` keeps them all, because "what is coming to this stop" is a different
    question, and the only one a feed whose trip ids do not match the schedule can
    answer at all."""
    delays, nxt, by_stop = parse_trip_updates(_trip_updates(1_700_000_000))
    assert delays == {"T1": 90}
    assert nxt == {"T1": {"stop": "S10", "seq": 5, "eta": 1_700_000_120}}
    assert by_stop["S10"] == [{"route": "R1", "trip": "T1", "eta": 1_700_000_120, "seq": 5}]
    assert by_stop["S11"] == [{"route": "R1", "trip": "T1", "eta": 1_700_000_300, "seq": 6}]
    # Soonest first, so a caller can take the head of the list.
    for arrivals in by_stop.values():
        assert arrivals == sorted(arrivals, key=lambda a: a["eta"])


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


def test_a_feed_that_turns_us_away_is_left_alone_and_backs_off():
    """TransMilenio's proxy rate-limits with 429 and, for nine days in September, answered 403 to a
    Railway address polling every twelve seconds. Polling straight through a refusal neither helps
    nor ends it, so each URL steps back on its own."""
    b = FeedBackoff()
    url = "https://example.test/positions.pb"
    now = 1_000_000.0
    assert b.blocked(url, now) is False

    first = b.refused(url, 429, now)
    assert first == BACKOFF_START_S and b.blocked(url, now + first - 1)
    assert b.blocked(url, now + first + 1) is False          # the pause ends on its own
    assert b.snapshot(now)[url] == {"status": 429, "forSeconds": int(first)}

    # a second refusal doubles the wait, up to a cap
    second = b.refused(url, 403, now)
    assert second == first * 2
    for _ in range(20):
        last = b.refused(url, 403, now)
    assert last == BACKOFF_MAX_S

    # being let in forgets all of it, so one bad minute does not punish the rest of the day
    b.allowed(url)
    assert b.blocked(url, now) is False and b.snapshot(now) == {}


def test_every_status_that_means_go_away_counts():
    assert {403, 429} <= set(TURNED_AWAY)
    assert 200 not in TURNED_AWAY and 500 not in TURNED_AWAY   # a server error is worth retrying
