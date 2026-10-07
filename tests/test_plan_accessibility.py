"""What the step-free search may claim, per city.

`/plan` has carried a `wheelchair` flag since v1.1 and passes it to OTP as
`preferences.accessibility.wheelchair.enabled`, which is the correct OTP 2.9 shape. Measured against
production on 2026-10-06, `wheelchair=true` and `wheelchair=false` returned the *same five
itineraries* for a long cross-town pair in Bogota, and `Itinerary.accessible` was null in both.

That is not a bug in the request. Every `router-config.json` sets `onlyConsiderAccessible: false`,
so unknown stops and trips cost extra instead of being dropped — the alternative, in a city with no
data, is zero itineraries, which reads as "no accessible service exists" and is false. The cost is
then applied uniformly to a network that is uniformly unknown, so the ranking does not move.

The routing is right and the silence is not. These warnings are how a client can say so.
"""
from app.normalize import set_feed_flags
from app.routers.plan import accessibility_warnings


def _codes(city: str, wheelchair: bool = True) -> list[str]:
    return [w.split(":", 1)[0] for w in accessibility_warnings(city, wheelchair)]


def test_a_city_that_surveyed_its_stops_warns_about_nothing():
    set_feed_flags("boston", {"accessibilitySupport": "verified"})
    assert _codes("boston") == []


def test_a_blanket_feed_value_is_called_out_rather_than_filtered_on():
    """Bogota publishes `wheelchair_boarding=1` for every stop. A filter would return the whole
    network stamped accessible, which is the one answer worse than no answer."""
    set_feed_flags("bogota", {"accessibilitySupport": "unverified"})
    assert _codes("bogota") == ["ACCESSIBILITY_UNVERIFIED"]


def test_a_city_with_no_accessibility_data_says_so():
    set_feed_flags("roma", {"accessibilitySupport": "none"})
    assert _codes("roma") == ["ACCESSIBILITY_NO_DATA"]


def test_an_unclassified_feed_under_claims_rather_than_over_claims():
    """A feed ingested before this flag existed has no `accessibilitySupport`. Treat that as no
    data: the cost of being wrong is a warning the rider did not need, not a step-free promise the
    network cannot keep."""
    set_feed_flags("lisboa", {})
    assert _codes("lisboa") == ["ACCESSIBILITY_NO_DATA"]


def test_nothing_is_said_when_the_rider_did_not_ask_for_step_free():
    for support in ("verified", "unverified", "none"):
        set_feed_flags("bogota", {"accessibilitySupport": support})
        assert accessibility_warnings("bogota", wheelchair=False) == []


def test_the_warning_carries_a_sentence_and_not_only_a_code():
    """The client renders `CODE: text`, the same contract `MODE_NO_VEHICLES` and
    `PARK_RIDE_NO_PARKING` already use."""
    set_feed_flags("bogota", {"accessibilitySupport": "unverified"})
    w = accessibility_warnings("bogota", True)[0]
    code, _, text = w.partition(": ")
    assert code == "ACCESSIBILITY_UNVERIFIED"
    assert len(text.split()) > 5
