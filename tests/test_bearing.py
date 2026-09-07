"""
Server-derived vehicle bearing.

Bogotá's GTFS-RT publishes no `bearing` on any vehicle, so the direction arrow and the marker tips would
never appear. The cache derives a bearing from consecutive positions; these tests pin the rules that keep
that honest — jitter ignored, stale pairs refused, and never a fabricated default.
"""
from __future__ import annotations

import collections

from app.geo import circular_mean, initial_bearing
from app.rt import MAX_PAIR_GAP_S, RTCache, derive_bearing

# Around Bogotá: ~0.001° latitude ≈ 111 m, ~0.001° longitude ≈ 111 m
LAT, LON = 4.6534, -74.0836


def trail(*points: tuple[float, float, int]) -> collections.deque:
    """(lon, lat, ts) oldest → newest, the shape the cache keeps per vehicle."""
    return collections.deque(points, maxlen=20)


def test_cardinal_bearings():
    assert round(initial_bearing(LAT, LON, LAT + 0.01, LON)) == 0        # north
    assert round(initial_bearing(LAT, LON, LAT, LON + 0.01)) == 90       # east
    assert round(initial_bearing(LAT, LON, LAT - 0.01, LON)) == 180      # south
    assert round(initial_bearing(LAT, LON, LAT, LON - 0.01)) == 270      # west


def test_derived_from_the_last_two_distinct_points():
    for delta, expected in [((0.001, 0.0), 0), ((0.0, 0.001), 90),
                            ((-0.001, 0.0), 180), ((0.0, -0.001), 270)]:
        dlat, dlon = delta
        t = trail((LON, LAT, 1000), (LON + dlon, LAT + dlat, 1030))
        assert round(derive_bearing(t)) == expected


def test_gps_jitter_while_parked_is_ignored():
    """A bus standing at a red light wobbles a few metres; that must not spin the arrow."""
    # ~2 m of wobble on top of a real northward move 30 s earlier
    t = trail((LON, LAT, 1000), (LON + 0.00002, LAT + 0.001, 1030), (LON, LAT + 0.001, 1060))
    b = derive_bearing(t)
    assert b is not None and round(b) == 0        # still the real northward course, not the jitter
    # nothing but jitter: no usable pair at all
    only_noise = trail((LON, LAT, 1000), (LON + 0.00002, LAT, 1030), (LON, LAT + 0.00002, 1060))
    assert derive_bearing(only_noise) is None


def test_a_pair_too_far_apart_in_time_is_refused():
    """Across a long gap the bus may have turned, so a straight line between the points would lie."""
    fresh = trail((LON, LAT, 1000), (LON, LAT + 0.001, 1000 + MAX_PAIR_GAP_S - 1))
    stale = trail((LON, LAT, 1000), (LON, LAT + 0.001, 1000 + MAX_PAIR_GAP_S + 1))
    assert derive_bearing(fresh) is not None
    assert derive_bearing(stale) is None


def test_no_history_gives_no_bearing():
    assert derive_bearing(None) is None
    assert derive_bearing(trail()) is None
    assert derive_bearing(trail((LON, LAT, 1000))) is None      # a single fix says nothing about heading


def test_circular_mean_handles_the_wrap_at_north():
    assert round(circular_mean([350.0, 10.0])) == 0             # not 180
    assert round(circular_mean([80.0, 100.0])) == 90
    assert round(circular_mean([42.0])) == 42
    assert circular_mean([0.0, 180.0]) == 180.0                 # opposite: keep the newest, don't invent


# ------------------------------------------------------------------ cache integration
def _cache(city) -> RTCache:
    return RTCache(city)


def _frame(cache: RTCache, ents: list[dict]) -> list[dict]:
    """Run one poll cycle's worth of bookkeeping without needing a protobuf feed."""
    cache._record_history(ents)
    cache._apply_bearings(ents)
    cache.by_id = {e["id"]: e for e in ents}
    return ents


def _ent(vid: str, lat: float, lon: float, ts: int, bearing: float | None = None) -> dict:
    return {"id": vid, "lat": lat, "lon": lon, "ts": ts, "bearing": bearing,
            "routeId": "R1", "tripId": "T1", "tripResolved": True, "label": None,
            "stopId": None, "stopSequence": None, "occupancy": None}


def test_cache_derives_and_labels_the_source(bogota):
    c = _cache(bogota)
    _frame(c, [_ent("v1", LAT, LON, 1000)])
    assert c.by_id["v1"]["bearing"] is None and c.by_id["v1"]["bearingSource"] is None
    ents = _frame(c, [_ent("v1", LAT + 0.001, LON, 1030)])
    assert round(ents[0]["bearing"]) == 0 and ents[0]["bearingSource"] == "derived"
    public = c.public_vehicle(ents[0])
    assert public["bearingSource"] == "derived" and public["bearing"] is not None


def test_a_bearing_from_the_feed_always_wins(bogota):
    c = _cache(bogota)
    _frame(c, [_ent("v1", LAT, LON, 1000)])
    ents = _frame(c, [_ent("v1", LAT + 0.001, LON, 1030, bearing=270.0)])   # moving north, feed says west
    assert ents[0]["bearing"] == 270.0 and ents[0]["bearingSource"] == "feed"


def test_published_bearing_is_smoothed_between_frames(bogota):
    """The icon should not twitch: the published value averages the last two derived bearings."""
    c = _cache(bogota)
    _frame(c, [_ent("v1", LAT, LON, 1000)])
    _frame(c, [_ent("v1", LAT + 0.001, LON, 1030)])                       # due north
    ents = _frame(c, [_ent("v1", LAT + 0.001, LON + 0.001, 1060)])        # now due east
    assert ents[0]["bearingSource"] == "derived"
    assert 40 < ents[0]["bearing"] < 50          # halfway between 0° and 90°, not a jump straight to 90°


def test_sse_delta_carries_a_bearing_change(bogota):
    c = _cache(bogota)
    first = _frame(c, [_ent("v1", LAT, LON, 1000)])
    prev = {e["id"]: dict(e) for e in first}
    second = _frame(c, [_ent("v1", LAT + 0.001, LON, 1030)])
    delta = c._compute_delta(prev, second)
    assert [e["id"] for e in delta["upd"]] == ["v1"]
    assert c.public_vehicle(delta["upd"][0])["bearingSource"] == "derived"
    # a vehicle that has not moved at all is not resent
    third = _frame(c, [_ent("v1", LAT + 0.001, LON, 1060)])
    assert c._compute_delta({e["id"]: dict(e) for e in second}, third)["upd"] == []


def test_forgotten_vehicles_release_their_smoothing_state(bogota):
    """Smoothing state must be pruned with the trail it belongs to, or it leaks for every retired bus."""
    c = _cache(bogota)
    _frame(c, [_ent("v0", LAT, LON, 1000)])
    _frame(c, [_ent("v0", LAT + 0.001, LON, 1030)])
    assert "v0" in c._last_derived
    # inflate past the cleanup threshold (len(history) > 3 * max(len(ents), 1000)) with retired vehicles
    for i in range(3100):
        gone = f"old{i}"
        c.history[gone] = trail((LON, LAT, 1000), (LON, LAT + 0.001, 1030))
        c._last_derived[gone] = 0.0
    c._record_history([_ent("v0", LAT + 0.002, LON, 1060)])
    assert set(c.history) == {"v0"}
    assert set(c._last_derived) == {"v0"}          # pruned in step with the trails
