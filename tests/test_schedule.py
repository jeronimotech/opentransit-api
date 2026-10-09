"""Summarising a published timetable the way a rider reads it.

Asked for by TransMilenio against 1.16.0 (1.12): the route page showed the first and last
departure, which answers "does it run now" and not "how long will I wait".
"""
from app.schedule import (
    FREQUENT_HEADWAY_MINUTES,
    bands,
    direction_group,
    hhmm,
    schedule_summary,
)


def _at(*clock: str) -> list[int]:
    out = []
    for c in clock:
        h, m = c.split(":")
        out.append(int(h) * 3600 + int(m) * 60)
    return out


def test_clock_formatting():
    assert hhmm(0) == "00:00"
    assert hhmm(5 * 3600 + 10 * 60) == "05:10"
    # GTFS writes a 01:10 departure on a service that began the previous day as 25:10.
    assert hhmm(25 * 3600 + 10 * 60) == "01:10"


def test_an_empty_day_is_not_a_frequent_service():
    s = schedule_summary([])
    assert s["trips"] == 0
    assert s["first"] is None
    assert s["frequent"] is False
    assert s["bands"] == []


def test_first_last_and_count():
    s = schedule_summary(_at("05:10", "22:45", "12:00"))
    assert (s["first"], s["last"], s["trips"]) == ("05:10", "22:45", 3)


def test_a_trunk_route_reads_as_frequent():
    deps = _at(*[f"06:{m:02d}" for m in range(0, 60, 5)], *[f"07:{m:02d}" for m in range(0, 60, 5)])
    s = schedule_summary(deps)
    assert s["typicalHeadwayMinutes"] == 5
    assert s["frequent"] is True


def test_a_feeder_with_long_gaps_does_not():
    s = schedule_summary(_at("06:00", "07:00", "08:00", "17:00", "18:00"))
    assert s["typicalHeadwayMinutes"] == 60
    assert s["frequent"] is False
    assert FREQUENT_HEADWAY_MINUTES < 60


def test_the_gap_belongs_to_the_hour_the_rider_is_waiting_in():
    """06:50 -> 07:20 is a thirty-minute wait for someone standing there at ten to seven, not for
    someone arriving at seven."""
    b = {x["hour"]: x for x in bands(_at("06:50", "07:20", "07:30"))}
    assert b[6]["headwayMinutes"]["typical"] == 30
    assert b[7]["headwayMinutes"]["typical"] == 10


def test_the_last_departure_contributes_no_interval():
    b = {x["hour"]: x for x in bands(_at("05:00", "22:40"))}
    assert b[22]["trips"] == 1
    assert b[22]["headwayMinutes"] is None


def test_the_typical_wait_survives_a_pre_dawn_and_a_late_night_run():
    """One bus at 04:40, the long gap after it and one at 23:50 must not make a five-minute trunk
    route look hourly — which is what a mean would have done."""
    deps = _at("04:40", *[f"06:{m:02d}" for m in range(0, 60, 5)],
               *[f"07:{m:02d}" for m in range(0, 60, 5)], "23:50")
    s = schedule_summary(deps)
    assert s["typicalHeadwayMinutes"] == 5
    assert s["frequent"] is True


def test_bands_come_out_in_clock_order():
    b = bands(_at("22:00", "06:00", "14:00"))
    assert [x["hour"] for x in b] == [6, 14, 22]
    assert [x["from"] for x in b] == ["06:00", "14:00", "22:00"]
    assert b[-1]["to"] == "23:00"


def test_a_pattern_that_runs_once_has_no_interval_at_all():
    s = schedule_summary(_at("06:00"))
    assert s["trips"] == 1
    assert s["typicalHeadwayMinutes"] is None
    assert s["frequent"] is False


def test_duplicate_departures_are_kept_as_two_buses():
    """Two vehicles leaving at the same minute is a zero-minute interval, which is real: the feed
    says the terminal dispatches two at once."""
    s = schedule_summary(_at("06:00", "06:00", "06:10", "06:20"))
    assert s["trips"] == 4
    assert s["bands"][0]["headwayMinutes"]["min"] == 0


class TestDirectionsNotPatterns:
    """What the first sandbox call taught: a feed's "pattern" is a shape, not a direction.

    GA547 publishes five near-identical variants; on the day this was measured ::01, ::03 and ::04
    ran (24, 24 and 15 departures) and ::02 and ::05 did not. Reading the schedule off the longest
    variant answered "0 departures" for a route running every twenty minutes.
    """

    @staticmethod
    def _p(code: str, headsign: str, stops: int, direction=None) -> dict:
        return {"code": code, "headsign": headsign, "directionId": direction,
                "stops": [{"gtfsId": f"s{i}"} for i in range(stops)]}

    def test_every_variant_of_one_direction_is_counted(self):
        pats = [self._p(f"r::0{i}", "Bosa San José", 112 - i) for i in range(1, 6)]
        group = direction_group(pats, "GA547")
        assert [p["code"] for p in group] == ["r::01", "r::02", "r::03", "r::04", "r::05"]

    def test_the_longest_variant_leads(self):
        group = direction_group([self._p("short", "Norte", 3), self._p("long", "Norte", 40)], "B74")
        assert group[0]["code"] == "long"

    def test_the_other_direction_is_a_different_group(self):
        pats = [self._p("north", "Portal Norte", 30), self._p("south", "Portal Sur", 32)]
        group = direction_group(pats, "B10")
        # The main direction is the one with the longest variant, and it is alone.
        assert [p["code"] for p in group] == ["south"]

    def test_asking_for_one_variant_returns_its_whole_direction(self):
        """The app passes the pattern the rider selected; the answer must still be the direction,
        or it is the bug this replaced."""
        pats = [self._p("a", "Norte", 10), self._p("b", "Norte", 9), self._p("c", "Sur", 11)]
        assert [p["code"] for p in direction_group(pats, "X", wanted="b")] == ["a", "b"]
        assert [p["code"] for p in direction_group(pats, "X", wanted="c")] == ["c"]

    def test_direction_ids_split_a_shared_headsign(self):
        pats = [self._p("out", "Centro", 20, direction=0), self._p("back", "Centro", 21, direction=1)]
        assert [p["code"] for p in direction_group(pats, "X")] == ["back"]

    def test_an_unknown_variant_is_not_silently_the_main_one(self):
        assert direction_group([self._p("a", "Norte", 10)], "X", wanted="nope") == []

    def test_no_patterns_no_group(self):
        assert direction_group([], "X") == []
