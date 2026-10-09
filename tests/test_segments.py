"""Equivalent services for a segment.

Asked for by TransMilenio against 1.16.0 (1.1): the itinerary names one route per leg, so a rider at
a trunk station lets three buses that would have served them go by. The station shapes in here are
San Victorino's as the Bogotá feed publishes them: a parent station (bogota:10006) with one stop per
platform, where routes along the same corridor board at different platforms.
"""
from app.segments import equivalent_services, natural_key, stop_family

STATION = [
    {"stop_id": "10006", "name": "San Victorino", "stop_code": None, "location_type": 1, "parent_station": None},
    {"stop_id": "61988", "name": "San Victorino A - 3 ó 6", "stop_code": "L063", "location_type": 0,
     "parent_station": "10006"},
    {"stop_id": "61992", "name": "San Victorino C - 4 ó 6", "stop_code": "L067", "location_type": 0,
     "parent_station": "10006"},
    {"stop_id": "57866", "name": "Estación San Victorino", "stop_code": "786A00_TM", "location_type": 0,
     "parent_station": None},
]


def _s(sid: str, name: str = "") -> dict:
    return {"id": sid, "name": name or sid, "code": None}


def _p(short: str, stops: list[str], headsign: str = "Norte", direction: int = 0, component: str = "trunk") -> dict:
    return {"route": {"id": f"bogota:{short}", "shortName": short, "component": component, "mode": "BUS"},
            "headsign": headsign, "directionId": direction, "stops": [_s(s) for s in stops]}


def test_a_route_serving_the_segment_is_an_alternative():
    out = equivalent_services([_p("B74", ["a", "b", "c", "d"])], {"a"}, {"d"})
    assert [s["shortName"] for s in out] == ["B74"]
    assert out[0]["boardAt"]["id"] == "a"
    assert out[0]["getOffAt"]["id"] == "d"
    assert out[0]["stops"] == 3


def test_the_other_direction_is_not_an_alternative():
    """The check that makes this feature safe: B74 southbound calls at both stops and takes the rider
    the wrong way. Pattern order is the direction check."""
    out = equivalent_services([_p("B74", ["d", "c", "b", "a"], direction=1)], {"a"}, {"d"})
    assert out == []


def test_a_branch_that_only_reaches_the_boarding_stop_is_not_an_alternative():
    out = equivalent_services([_p("C19", ["a", "b", "x", "y"])], {"a"}, {"d"})
    assert out == []


def test_platforms_of_one_station_are_the_same_segment():
    """H13 boards at platform A, J72 at platform C, both reach the destination station. A rider who
    only hears about one of them stands at the wrong platform."""
    out = equivalent_services(
        [_p("H13", ["61988", "mid", "99999"]), _p("J72", ["61992", "mid", "99998"])],
        {"61988", "61992", "10006"}, {"99999", "99998"})
    assert [(s["shortName"], s["boardAt"]["id"]) for s in out] == [("H13", "61988"), ("J72", "61992")]


def test_the_legs_own_route_is_left_out():
    out = equivalent_services([_p("B74", ["a", "d"]), _p("C19", ["a", "d"])],
                              {"a"}, {"d"}, exclude={"bogota:B74"})
    assert [s["shortName"] for s in out] == ["C19"]


def test_the_express_wins_over_its_local():
    """Two patterns of one route from one platform: the rider wants the one with fewer stops, and
    both are "B74", so showing both would read as a duplicate."""
    out = equivalent_services([_p("B74", ["a", "b", "c", "d"]), _p("B74", ["a", "d"])], {"a"}, {"d"})
    assert len(out) == 1
    assert out[0]["stops"] == 1


def test_a_loop_boards_at_the_first_call_and_alights_after_it():
    """A loop pattern passes the destination before the origin and again after it. Boarding at the
    first call at the origin is what a rider can actually do."""
    out = equivalent_services([_p("K40", ["d", "a", "b", "d"])], {"a"}, {"d"})
    assert len(out) == 1
    assert out[0]["stops"] == 2


def test_order_reads_like_signage():
    out = equivalent_services([_p("B74", ["a", "d"]), _p("B9", ["a", "d"]), _p("B100", ["a", "d"])], {"a"}, {"d"})
    assert [s["shortName"] for s in out] == ["B9", "B74", "B100"]


def test_components_group_together():
    out = equivalent_services([_p("9-3", ["a", "d"], component="feeder"), _p("B9", ["a", "d"])], {"a"}, {"d"})
    assert [s["component"] for s in out] == ["feeder", "trunk"]


def test_a_pattern_without_a_route_is_skipped():
    """OTP can answer with a null route on a pattern it is still indexing; it must not become a
    nameless chip in the app."""
    out = equivalent_services([{"route": None, "stops": [_s("a"), _s("d")]}], {"a"}, {"d"})
    assert out == []


class TestStopFamily:
    def test_a_platform_brings_its_siblings_and_its_station(self):
        assert stop_family(STATION, "61988") == {"61988", "61992", "10006"}

    def test_a_station_brings_its_platforms(self):
        assert stop_family(STATION, "10006") == {"10006", "61988", "61992"}

    def test_a_stop_without_a_station_stays_alone(self):
        assert stop_family(STATION, "57866") == {"57866"}

    def test_an_unknown_stop_is_its_own_family(self):
        assert stop_family(STATION, "99999") == {"99999"}


def test_natural_key_sorts_digits_as_numbers():
    assert sorted(["B74", "B9", "B100"], key=natural_key) == ["B9", "B74", "B100"]
    assert sorted(["9-3", "9-10", "9-4"], key=natural_key) == ["9-3", "9-4", "9-10"]


class TestWhatTheLiveFeedTaught:
    """Both of these came out of the first sandbox call, against San Victorino."""

    def test_a_route_that_loops_half_the_city_first_is_not_an_alternative(self):
        """Measured: the same stop pair served in 5 calls by one route and in 31 by another.
        Boarding the second because it came first would cost the rider the trip."""
        out = equivalent_services(
            [_p("GA547", ["a"] + [f"s{i}" for i in range(4)] + ["d"]),
             _p("GA506", ["a"] + [f"x{i}" for i in range(30)] + ["d"])],
            {"a"}, {"d"})
        assert [s["shortName"] for s in out] == ["GA547"]

    def test_a_local_is_still_an_alternative_to_an_express(self):
        out = equivalent_services(
            [_p("X1", ["a", "d"]), _p("X2", ["a", "m1", "m2", "d"])], {"a"}, {"d"})
        assert {s["shortName"] for s in out} == {"X1", "X2"}

    def test_the_feeds_duplicate_rows_collapse_to_one_chip(self):
        """Bogotá lists GA506 three times with different ids; three identical chips read as a bug.

        The long names differ only in ways a rider cannot act on, and the segment is already fixed,
        so this is one instruction: take GA506 from here.
        """
        dupes = [
            {"route": {"id": f"bogota:{i}", "shortName": "GA506", "longName": name,
                       "component": "zonal", "mode": "BUS"},
             "headsign": None, "directionId": 0, "stops": [_s("a"), _s("m"), _s("d")]}
            for i, name in ((111, "Molinos - Centro"), (222, "Molinos-Centro"), (333, "Molinos - Centro"))
        ]
        out = equivalent_services(dupes, {"a"}, {"d"})
        assert len(out) == 1
        assert out[0]["shortName"] == "GA506"
        assert len(out[0]["mergedIds"]) == 2

    def test_a_service_that_does_not_run_today_is_not_an_alternative(self):
        """Measured on a Thursday: Bogotá publishes a second row per route for the Sunday ciclovía,
        same number with "Ciclovía" appended, and it showed up as a duplicate chip — really a bus
        that would never arrive. `hasServiceToday` already knew."""
        rows = [
            {"route": {"id": "bogota:12660", "shortName": "A134", "longName": "Pq. Central Bavaria",
                       "component": "dual", "mode": "BUS",
                       "serviceWindow": {"hasServiceToday": True, "start": "04:00", "end": "22:50"}},
             "headsign": None, "directionId": 0, "stops": [_s("a"), _s("d")]},
            {"route": {"id": "bogota:12661", "shortName": "A134",
                       "longName": "Pq. Central Bavaria Ciclovía", "component": "dual", "mode": "BUS",
                       "serviceWindow": {"hasServiceToday": False, "start": None, "end": None}},
             "headsign": None, "directionId": 0, "stops": [_s("a"), _s("d")]},
        ]
        out = equivalent_services(rows, {"a"}, {"d"})
        assert [s["id"] for s in out] == ["bogota:12660"]

    def test_a_service_with_no_window_at_all_is_kept(self):
        """Absent is not the same as not running — a city whose feed has no calendar would otherwise
        answer nothing at all."""
        rows = [{"route": {"id": "r1", "shortName": "X", "component": "bus", "mode": "BUS"},
                 "headsign": None, "directionId": 0, "stops": [_s("a"), _s("d")]}]
        assert len(equivalent_services(rows, {"a"}, {"d"})) == 1

    def test_the_same_name_at_two_platforms_stays_two_instructions(self):
        """The reason the collapse is per platform: merging these would send a rider to the wrong
        vagón, which is worse than showing the name twice."""
        rows = [
            {"route": {"id": f"bogota:{i}", "shortName": "GA506", "longName": "Molinos - Centro",
                       "component": "zonal", "mode": "BUS"},
             "headsign": None, "directionId": 0, "stops": [_s(board), _s("d")]}
            for i, board in ((111, "61988"), (222, "61992"))
        ]
        out = equivalent_services(rows, {"61988", "61992"}, {"d"})
        assert sorted((s["boardAt"]["id"]) for s in out) == ["61988", "61992"]
