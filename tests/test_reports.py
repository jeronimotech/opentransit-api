"""Rider reports: what the app got wrong, from the person who found it.

Asked for by TransMilenio against 1.16.0 (1.16). The tests that matter here are the ones about what
a report may *not* carry: anonymity is the feature, and it has to be a property of the code rather
than of how the client happens to call it.
"""
from pathlib import Path

import pytest

from app.cities import load_city_file
from app.reports import MAX_MESSAGE, clean_report


def test_a_report_keeps_what_the_rider_typed():
    row = clean_report({"kind": "wrong_info", "message": "El paradero está al otro lado de la calle",
                        "stopId": "bogota:57866", "routeId": "bogota:B74",
                        "appVersion": "1.17.0", "locale": "es"})
    assert row["kind"] == "wrong_info"
    assert row["stop_id"] == "bogota:57866"
    assert row["route_id"] == "bogota:B74"
    assert row["app_version"] == "1.17.0"


def test_the_row_has_no_field_for_anything_identifying():
    """The shape itself is the guarantee: there is nowhere to put a device id or a position, so no
    client change and no later caller can turn this into a tracker."""
    row = clean_report({"kind": "barrier", "message": "La rampa está bloqueada",
                        "deviceId": "abc", "lat": 4.6, "lon": -74.08, "userId": "u1"})
    assert set(row) == {"kind", "message", "stop_id", "route_id", "app_version", "locale", "contact"}


def test_contact_is_kept_only_because_the_rider_typed_it():
    assert clean_report({"kind": "other", "message": "x", "contact": "a@b.co"})["contact"] == "a@b.co"
    assert clean_report({"kind": "other", "message": "x"})["contact"] is None
    # Blank is the same as absent: an empty field should not look like an answer.
    assert clean_report({"kind": "other", "message": "x", "contact": "   "})["contact"] is None


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="kind"):
        clean_report({"kind": "spam", "message": "x"})


def test_a_report_without_a_message_is_refused():
    with pytest.raises(ValueError, match="message"):
        clean_report({"kind": "barrier", "message": "   "})


def test_a_message_longer_than_the_cap_is_refused_rather_than_truncated():
    """Silently cutting a report in half loses the half that said where the problem is."""
    with pytest.raises(ValueError, match="message"):
        clean_report({"kind": "other", "message": "x" * (MAX_MESSAGE + 1)})


def test_a_missing_kind_is_other_rather_than_an_error():
    assert clean_report({"message": "x"})["kind"] == "other"


class TestSupportChannels:
    """1.16 also asked for the channels to say what they answer, and for the emergency line."""

    def test_bogota_describes_every_channel(self):
        city = load_city_file(Path("cities/bogota.yaml"))
        assert city.services
        assert all(s.description for s in city.services)

    def test_the_emergency_line_dials_instead_of_opening_a_page(self):
        city = load_city_file(Path("cities/bogota.yaml"))
        em = [s for s in city.services if s.emergency]
        assert [s.phone for s in em] == ["123"]
        assert all(s.kind == "call" and s.url is None for s in em)

    def test_a_call_channel_without_a_number_is_a_config_error(self):
        from app.cities import ServiceTile
        with pytest.raises(ValueError, match="phone"):
            ServiceTile(id="emergency", label="x", kind="call")

    def test_a_page_channel_still_needs_its_url(self):
        from app.cities import ServiceTile
        with pytest.raises(ValueError, match="url"):
            ServiceTile(id="pqrs", label="x")
