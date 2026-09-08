"""Addresses must survive search, or no multimodal trip can start from a door.

Two independent defects made every address query return unrelated stops:
  1. Photon rejected our requests with 403 because httpx sent its default User-Agent,
     and the failure was swallowed into a stops-only result.
  2. Even with Photon answering, every GTFS station outranked every address, so
     truncating to the page size dropped the addresses again.
"""
import httpx
import pytest

from app.config import settings
from app.geocode import (
    MIN_PLACE_SLOTS,
    _collapse_streets,
    _geocoder_headers,
    _reserve_place_slots,
    looks_like_address,
    rank_results,
)


def _place(name, typ="street", lat=0.0, lon=0.0):
    return {"name": name, "type": typ, "source": "photon", "lat": lat, "lon": lon}


def _stop(name, typ="station"):
    return {"name": name, "type": typ, "source": "gtfs", "_nRoutes": 3, "lat": 0.0, "lon": 0.0}


@pytest.mark.parametrize("q", ["Calle 85 #12-30", "Carrera 7 No 72-41", "Av 68 # 24 - 10",
                               "Cra 13 n 45-67", "221B Baker Street"])
def test_house_numbers_are_recognised(q):
    assert looks_like_address(q)


@pytest.mark.parametrize("q", ["Museo del Oro", "Portal Sur", "Zona T", "Universidad Nacional"])
def test_plain_names_are_not_addresses(q):
    assert not looks_like_address(q)


def test_address_query_puts_the_address_above_similar_stations():
    rs = [_stop("Calle 34"), _stop("Calle 19"), _place("Calle 85"), _stop("Calle 63")]
    assert rank_results(rs, "Calle 85 #12-30")[0]["name"] == "Calle 85"


def test_name_query_still_prefers_the_station():
    rs = [_stop("Museo del Oro"), _place("Museo del Oro", typ="poi")]
    assert rank_results(rs, "Museo del Oro")[0]["source"] == "gtfs"


def test_places_keep_slots_when_stops_would_fill_the_page():
    ranked = [_stop(f"Calle {i}") for i in range(8)] + [_place("Calle 85"), _place("Calle 85A")]
    out = _reserve_place_slots(ranked, 8)
    assert len(out) == 8
    assert sum(1 for r in out if r["source"] == "photon") == min(MIN_PLACE_SLOTS, 2)


def test_reserving_slots_never_grows_or_reorders_the_page():
    ranked = [_stop("A"), _place("B"), _place("C"), _place("D"), _stop("E")]
    out = _reserve_place_slots(ranked, 3)
    assert len(out) == 3
    assert [r["name"] for r in out] == sorted([r["name"] for r in out],
                                              key=lambda n: [x["name"] for x in ranked].index(n))


def test_street_segments_collapse_to_the_one_nearest_the_user():
    class _C:
        id = "t"

        class center:
            lat, lon = 4.61, -74.08
    far, near = _place("Calle 85", lat=4.7086, lon=-74.1008), _place("Calle 85", lat=4.6730, lon=-74.0660)
    out = _collapse_streets(_C, [far, near], 4.6721, -74.0592)
    assert [r["name"] for r in out] == ["Calle 85"]
    assert out[0]["lat"] == pytest.approx(4.6730)


def test_points_of_interest_are_not_collapsed_by_name():
    class _C:
        id = "t"

        class center:
            lat, lon = 4.61, -74.08
    a, b = _place("Éxito", typ="poi", lat=4.60, lon=-74.08), _place("Éxito", typ="poi", lat=4.70, lon=-74.10)
    assert len(_collapse_streets(_C, [a, b], None, None)) == 2


def test_requests_identify_themselves():
    """The public Photon instance answers 403 to a bare library User-Agent."""
    ua = _geocoder_headers()["User-Agent"]
    assert ua and "httpx" not in ua.lower() and "python" not in ua.lower()
    assert ua == settings().GEOCODER_USER_AGENT


def test_the_default_client_would_be_rejected():
    """Guards the regression directly: httpx's own default is what earned the 403."""
    assert "httpx" in httpx.Client().headers["user-agent"].lower()
