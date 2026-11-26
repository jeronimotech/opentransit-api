"""Unit tests for scripts/rt_match.py :: is_stale() and confirm_stale().

The freeze these guard against is the 2026-11-26 TransMilenio outage: the positions feed kept
answering 200 with ~6,700 vehicles, but every timestamp stayed pinned at 19:18 the night before,
so overlap read 100 % and rtById read 95.7 % on fossil positions. The drift and shrink checks
cannot see it; only entity age can. The one trap to avoid is the overnight one (the 2026-11-23
Roma lesson): a feed that is legitimately empty at 3am must not read as frozen.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("rt_match", ROOT / "scripts" / "rt_match.py")
rt_match = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt_match)
is_stale = rt_match.is_stale


def row(*, age=60.0, vehicles=2500, error=None):
    """A sampled /health row shaped like sample() returns, with the two knobs is_stale() reads."""
    if error is not None:
        return {"city": "bogota", "error": error}
    return {"city": "bogota", "ageP50": age, "vehicles": vehicles}


def test_fresh_feed_is_not_stale():
    assert is_stale(row(age=60.0, vehicles=6700)) is False


def test_frozen_feed_with_a_full_fleet_is_stale():
    # The 2026-11-26 case: 12.7h of fossils, but a full fleet still reporting.
    assert is_stale(row(age=45865.0, vehicles=6742)) is True


def test_empty_overnight_feed_is_not_stale():
    # Roma at 3am: old or no fresh entities, but the fleet has gone home. Not a freeze.
    assert is_stale(row(age=45865.0, vehicles=0)) is False


def test_a_thin_overnight_fleet_is_not_stale():
    # A handful of night owls below the fleet floor: still not a freeze.
    assert is_stale(row(age=20000.0, vehicles=4)) is False


def test_old_age_just_under_the_fleet_floor_is_not_stale():
    assert is_stale(row(age=20000.0, vehicles=9), min_vehicles=10) is False
    assert is_stale(row(age=20000.0, vehicles=10), min_vehicles=10) is True


def test_age_at_the_threshold_is_not_yet_stale():
    # Strictly greater-than, so the boundary itself holds.
    assert is_stale(row(age=900.0, vehicles=6700), max_age=900.0) is False
    assert is_stale(row(age=901.0, vehicles=6700), max_age=900.0) is True


def test_unreadable_health_is_not_stale():
    # An unreachable API is a different alarm; this check stays strictly about a frozen feed.
    assert is_stale(row(error="URLError: boom")) is False


def test_missing_age_is_not_stale():
    assert is_stale({"city": "casablanca", "vehicles": None, "ageP50": None}) is False


def test_disabled_realtime_city_is_not_stale():
    # Casablanca: realtime off, so no age and no vehicles ever. Never frozen.
    assert is_stale({"city": "casablanca", "ageP50": None, "vehicles": None}) is False


# --- confirm_stale: re-reads before failing, so one fluke sample cannot redden the alarm ---

def test_confirm_stale_true_only_when_every_read_is_stale(monkeypatch):
    frozen = row(age=45865.0, vehicles=6742)
    monkeypatch.setattr(rt_match, "sample", lambda base, city, timeout=30: frozen)
    assert rt_match.confirm_stale("x", "bogota", samples=3, interval=0) is True


def test_confirm_stale_false_when_a_later_read_recovers(monkeypatch):
    reads = iter([row(age=45865.0, vehicles=6742), row(age=30.0, vehicles=6742)])
    monkeypatch.setattr(rt_match, "sample", lambda base, city, timeout=30: next(reads))
    assert rt_match.confirm_stale("x", "bogota", samples=2, interval=0) is False


def test_confirm_stale_false_when_fleet_goes_home_between_reads(monkeypatch):
    # First read looks frozen; the next shows the fleet has emptied — it was dusk, not a freeze.
    reads = iter([row(age=45865.0, vehicles=6742), row(age=46000.0, vehicles=0)])
    monkeypatch.setattr(rt_match, "sample", lambda base, city, timeout=30: next(reads))
    assert rt_match.confirm_stale("x", "bogota", samples=2, interval=0) is False
