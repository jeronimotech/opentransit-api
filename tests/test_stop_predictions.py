"""Realtime for a feed whose trip ids are not the schedule's.

The TTC publishes trip ids that appear nowhere in Toronto's open-data GTFS — 0 of 590
matched when the city was added — so everything keyed on the trip is unusable and the
board would show scheduled times only. Its stop ids match 99.6%, so the predictions are
there; they have to be read by stop and paired by hand.

Pairing is where this could quietly lie, so these tests are mostly about what it
refuses to do.
"""
from __future__ import annotations

import datetime as dt

from app.cities import City
from app.normalize import apply_stop_predictions

NOW = 1_700_000_000


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat().replace("+00:00", "Z")


def _dep(route: str, sched: int, **over) -> dict:
    d = {"route": {"id": f"toronto:{route}", "mode": "BUS"}, "headsign": "North",
         "tripId": f"toronto:t-{sched}", "scheduledTime": _iso(sched), "realtimeTime": None,
         "realtime": False, "delaySeconds": None, "canceled": False, "vehicleId": None,
         "stopSequence": 1}
    d.update(over)
    return d


def _arr(route: str | None, eta: int, **over) -> dict:
    a = {"route": route, "trip": f"rt-{eta}", "eta": eta, "seq": 1}
    a.update(over)
    return a


def test_a_prediction_lands_on_the_scheduled_departure_it_belongs_to(bogota: City):
    deps = [_dep("501", NOW + 600)]
    out = apply_stop_predictions(deps, [_arr("501", NOW + 780)], bogota, NOW)
    assert len(out) == 1
    assert out[0]["realtime"] is True and out[0]["realtimeSource"] == "stop"
    assert out[0]["realtimeTime"] == _iso(NOW + 780)
    assert out[0]["delaySeconds"] == 180


def test_a_prediction_for_another_route_is_never_borrowed(bogota: City):
    """Two routes call at the same stop; using one's prediction for the other would be
    a plausible-looking lie."""
    deps = [_dep("501", NOW + 600)]
    out = apply_stop_predictions(deps, [_arr("504", NOW + 610)], bogota, NOW)
    scheduled = [d for d in out if d["tripId"]]
    assert scheduled[0]["realtime"] is False, "a 504 prediction was attached to a 501"
    # ...and it is still reported, because a bus really is coming.
    extra = [d for d in out if d["tripId"] is None]
    assert len(extra) == 1 and extra[0]["route"]["id"].endswith("504")


def test_a_prediction_far_from_any_schedule_is_added_rather_than_forced(bogota: City):
    deps = [_dep("501", NOW + 600)]
    out = apply_stop_predictions(deps, [_arr("501", NOW + 5400)], bogota, NOW)
    assert len(out) == 2
    assert out[0]["realtime"] is False, "an hour-and-a-half gap was treated as the same bus"
    extra = [d for d in out if d["tripId"] is None][0]
    assert extra["scheduledTime"] is None, "an arrival with no schedule invented one"
    assert extra["realtimeTime"] == _iso(NOW + 5400)


def test_each_prediction_is_used_once(bogota: City):
    """Two buses on one route: one prediction must not explain both departures."""
    deps = [_dep("501", NOW + 600), _dep("501", NOW + 900)]
    out = apply_stop_predictions(deps, [_arr("501", NOW + 620)], bogota, NOW)
    used = [d for d in out if d.get("realtimeSource") == "stop" and d["tripId"]]
    assert len(used) == 1
    assert used[0]["scheduledTime"] == _iso(NOW + 600), "it paired with the further departure"


def test_predictions_already_in_the_past_are_dropped(bogota: City):
    deps = [_dep("501", NOW + 600)]
    out = apply_stop_predictions(deps, [_arr("501", NOW - 900)], bogota, NOW)
    assert len(out) == 1 and out[0]["realtime"] is False


def test_the_source_of_a_realtime_time_is_always_stated(bogota: City):
    """A client should never have to guess whether a time came from a matched trip or
    from pairing by stop — they are not equally certain."""
    deps = [_dep("501", NOW + 600)]
    out = apply_stop_predictions(deps, [_arr("501", NOW + 700)], bogota, NOW)
    assert out[0]["realtimeSource"] == "stop"
    extra = apply_stop_predictions([], [_arr("501", NOW + 700)], bogota, NOW)
    assert extra[0]["realtimeSource"] == "stop" and extra[0]["realtimeTime"]


def test_a_departure_that_already_has_realtime_is_left_alone(bogota: City):
    """Where the trip ids do match, OTP has already done this properly."""
    deps = [_dep("501", NOW + 600, realtime=True, realtimeTime=_iso(NOW + 660))]
    out = apply_stop_predictions(deps, [_arr("501", NOW + 700)], bogota, NOW)
    assert out[0]["realtimeTime"] == _iso(NOW + 660)
    assert out[0].get("realtimeSource") is None


def test_no_predictions_changes_nothing(bogota: City):
    deps = [_dep("501", NOW + 600)]
    assert apply_stop_predictions(deps, [], bogota, NOW) == deps
