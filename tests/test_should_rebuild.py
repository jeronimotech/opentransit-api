"""Unit tests for scripts/should-rebuild.py :: decide().

The script name is hyphenated, so it is loaded by path (same pattern as test_rental_plan.py).
These cover the five verdicts and, in particular, the 2026-11-19 guard: a low rtById measured over
a dead positions feed is an upstream outage, not graph drift, and must not trigger a rebuild.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "should_rebuild", ROOT / "scripts" / "should-rebuild.py")
should_rebuild = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(should_rebuild)
decide = should_rebuild.decide


def health(*, drift_pct=100.0, rebuild=False, rt_by_id=95.0,
           age=120.0, vehicles=2500, shrink=None):
    """A /health shaped like production's, with knobs for the fields decide() reads."""
    return {
        "static": {"shrink": shrink},
        "realtime": {"pctTripResolvedById": rt_by_id,
                     "entityAgeP50Seconds": age, "vehicles": vehicles},
        "router": {"graphDrift": {"overlapPct": drift_pct, "rebuild": rebuild}},
    }


def test_unknown_when_health_unreadable():
    assert decide({"__error__": "boom"}, 60.0)["verdict"] == "unknown"


def test_ok_when_no_rebuild_flag():
    assert decide(health(rebuild=False), 60.0)["verdict"] == "ok"


def test_reingest_when_feed_shrank():
    h = health(rebuild=True, shrink={"lostPct": 48.0, "trips": 100, "previousTrips": 200})
    assert decide(h, 60.0)["verdict"] == "reingest"


def test_hold_when_no_realtime_feed():
    assert decide(health(rebuild=True, rt_by_id=None), 60.0)["verdict"] == "hold"


def test_hold_when_rtbyid_still_high():
    # drift flagged but realtime still resolves — the 2026-10-06 trap; do not rebuild.
    assert decide(health(rebuild=True, rt_by_id=92.0), 60.0)["verdict"] == "hold"


def test_rebuild_when_rtbyid_low_and_feed_healthy():
    r = decide(health(rebuild=True, rt_by_id=7.0, age=120.0, vehicles=2500), 60.0)
    assert r["verdict"] == "rebuild"
    assert not r.get("feedDown")


def test_hold_when_feed_stale_even_if_rtbyid_low():
    # The 2026-11-19 case: rtById 7% but the positions feed is serving ~12-day-old fossils.
    r = decide(health(rebuild=True, rt_by_id=7.0, age=1_069_854.0, vehicles=7), 60.0)
    assert r["verdict"] == "hold"
    assert r["feedDown"] is True


def test_hold_when_feed_empty_even_if_rtbyid_low():
    # Fresh-ish timestamp but almost no vehicles: still an outage, not drift.
    r = decide(health(rebuild=True, rt_by_id=5.0, age=100.0, vehicles=3), 60.0)
    assert r["verdict"] == "hold"
    assert r["feedDown"] is True


def test_rebuild_survives_a_lower_staleness_threshold():
    # With a stricter max-age the healthy feed still rebuilds; the guard only fires on real staleness.
    r = decide(health(rebuild=True, rt_by_id=7.0, age=120.0, vehicles=2500),
               60.0, rt_max_age=60.0)
    # age 120 > 60 -> now treated as stale -> hold (guard is tunable and does fire)
    assert r["verdict"] == "hold" and r["feedDown"] is True
