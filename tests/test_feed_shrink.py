"""Noticing that an ingest stored far less service than the version it replaced.

Measured 2026-10-14: our 24-hourly refresh read TransMilenio mid-publication and stored 110,648
trips where the finished feed had 179,919. The ingest was valid, every check was green, and because
those trip ids appear nowhere in the realtime feed, Bogotá showed a map of buses that never moved
for a day. These tests are the numbers from that day.
"""
from app.feed_shrink import SHRINK_THRESHOLD_PCT, feed_shrink


def test_the_bogota_incident_is_reported():
    s = feed_shrink(179_919, 110_648)
    assert s is not None
    assert s["lostPct"] == 38.5
    assert s["previousTrips"] == 179_919
    assert "110,648" in s["note"] and "179,919" in s["note"]


def test_a_feed_that_grew_is_not_a_problem():
    assert feed_shrink(110_648, 179_919) is None


def test_an_unchanged_feed_is_not_a_problem():
    assert feed_shrink(179_919, 179_919) is None


def test_ordinary_churn_stays_quiet():
    """Day-to-day movement on this feed runs to about 14 %; alarming on that would make the check
    noise, and a check that cries every morning is a check nobody reads."""
    assert feed_shrink(100_000, 86_000) is None
    assert SHRINK_THRESHOLD_PCT == 30.0


def test_the_threshold_is_the_boundary():
    assert feed_shrink(100_000, 70_001) is None          # 29.999 %
    assert feed_shrink(100_000, 70_000) is not None      # exactly 30 %


def test_a_first_ingest_has_nothing_to_compare_against():
    assert feed_shrink(None, 179_919) is None
    assert feed_shrink(0, 179_919) is None


def test_a_feed_that_went_empty_is_not_this_alarm():
    """Zero trips is a different illness — the ingest failed or the feed is broken — and reporting
    it as a shrink would put a percentage on something that needs a different fix."""
    assert feed_shrink(179_919, 0) is None
    assert feed_shrink(179_919, None) is None


def test_the_note_says_what_it_costs():
    """The point of the sentence is that whoever reads it at 06:17 knows it is not cosmetic."""
    note = feed_shrink(179_919, 110_648)["note"]
    assert "partial publication" in note
    assert "live arrivals" in note
