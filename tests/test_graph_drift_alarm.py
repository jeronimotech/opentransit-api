"""Unit tests for the realtime cross-check that gates graph_drift.py's drift alarm.

The 2026-11-19 lesson, applied to the alarm: a drop in feed overlap is only the signal to rebuild
once realtime has followed the agency to the new numbering. Until then (rtById still high) a rebuild
would collapse the match (the 2026-10-06 trap), and on a dead positions feed a low rtById says
nothing about drift at all. So the daily alarm must go red only when a rebuild is actionable today.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("graph_drift", ROOT / "scripts" / "graph_drift.py")
graph_drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(graph_drift)
realtime_signal = graph_drift.realtime_signal
drift_is_actionable = graph_drift.drift_is_actionable


def health(*, rt_by_id=100.0, age=75.0, vehicles=2500):
    return {"realtime": {"pctTripResolvedById": rt_by_id,
                         "entityAgeP50Seconds": age, "vehicles": vehicles}}


def test_signal_moved_when_rtbyid_low_on_a_live_feed():
    assert realtime_signal(health(rt_by_id=7.0, age=75.0, vehicles=2500)) == "moved"


def test_signal_steady_when_rtbyid_still_high():
    # brisbane/roma/lisboa on 2026-11-19: overlap down, but rtById ~100% -> realtime has not moved.
    assert realtime_signal(health(rt_by_id=99.8)) == "steady"


def test_signal_feeddown_when_feed_is_stale():
    # The positions feed serving ~12-day-old fossils: a low rtById measures a dead feed, not drift.
    assert realtime_signal(health(rt_by_id=7.0, age=1_069_854.0, vehicles=7)) == "feeddown"


def test_signal_feeddown_when_feed_is_nearly_empty():
    assert realtime_signal(health(rt_by_id=5.0, age=100.0, vehicles=3)) == "feeddown"


def test_signal_unknown_when_no_rtbyid():
    assert realtime_signal({"realtime": {}}) == "unknown"


def test_signal_unknown_when_health_unreadable():
    assert realtime_signal({"__error__": "URLError: boom"}) == "unknown"


def test_signal_threshold_is_tunable():
    # A stricter max-age turns an otherwise-live feed into a feed-outage verdict.
    assert realtime_signal(health(rt_by_id=7.0, age=120.0, vehicles=2500), rt_max_age=60.0) == "feeddown"
    # And a lower floor lets a middling rtById read as "not moved yet".
    assert realtime_signal(health(rt_by_id=55.0), rt_floor=50.0) == "steady"


def test_alarm_silent_when_every_drifted_city_is_steady_or_feeddown():
    # The whole point: no false red on a day when nothing is ours to rebuild.
    assert drift_is_actionable(["steady", "feeddown", "steady"]) is False


def test_alarm_fires_when_any_city_has_actually_moved():
    assert drift_is_actionable(["steady", "moved", "feeddown"]) is True


def test_alarm_fails_loud_when_health_is_unreadable():
    # An unreachable API on a drifted city must never go silent.
    assert drift_is_actionable(["steady", "unknown"]) is True


def test_alarm_silent_when_no_city_is_below_threshold():
    assert drift_is_actionable([]) is False


# --- 2026-11-23 Roma lesson: rtById noisy on a small fleet; one dip must not redden the alarm. ---

def test_consolidate_realtime_takes_best_rtbyid():
    merged = graph_drift.consolidate_realtime([
        health(rt_by_id=33.0, vehicles=200),
        health(rt_by_id=97.0, vehicles=201),
    ])
    assert merged["realtime"]["pctTripResolvedById"] == 97.0
    assert merged["realtime"]["vehicles"] == 201


def test_consolidate_realtime_ignores_errors_and_keeps_min_age():
    merged = graph_drift.consolidate_realtime([
        {"__error__": "boom"}, health(age=300.0), health(age=90.0)])
    assert merged["realtime"]["entityAgeP50Seconds"] == 90.0


def test_a_recovering_dip_reads_steady_not_moved():
    # Roma: one read 33%, one 97% -> best read says realtime has not moved -> no alarm.
    merged = graph_drift.consolidate_realtime([
        health(rt_by_id=33.0, vehicles=200), health(rt_by_id=97.0, vehicles=200)])
    assert graph_drift.realtime_signal(merged) == "steady"


def test_a_persistent_low_reads_moved():
    # bogota: low on every read -> genuinely moved -> alarm stands.
    merged = graph_drift.consolidate_realtime([
        health(rt_by_id=27.0, vehicles=6900), health(rt_by_id=28.0, vehicles=6900)])
    assert graph_drift.realtime_signal(merged) == "moved"
