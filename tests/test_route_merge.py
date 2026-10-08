"""Collapsing routes a feed lists twice — and refusing to collapse ones it does not.

The numbers in these tests are from production on 2026-10-08. The two cities matter because they
look identical from a distance and need opposite treatment: Brisbane repeats 728 entries with the
same short *and* long name, Boston repeats 205 short names and not one long one.
"""
from app.route_merge import merge_duplicate_routes


def _r(rid: str, short: str, long: str = "", component: str = "bus", mode: str = "BUS") -> dict:
    return {"id": rid, "shortName": short, "longName": long, "component": component, "mode": mode}


def test_identical_rows_collapse_to_one():
    """Brisbane: thirteen copies of BRBD · Brisbane City - Airport, identical in every way a rider
    can see. Feed bookkeeping, and nothing is lost by showing it once."""
    out = merge_duplicate_routes([
        _r("BRBD-5187", "BRBD", "Brisbane City - Airport"),
        _r("BRBD-5203", "BRBD", "Brisbane City - Airport"),
        _r("BRBD-4997", "BRBD", "Brisbane City - Airport"),
    ])
    assert len(out) == 1
    # The lowest id survives, so two ingests of an unchanged feed agree on which one it is.
    assert out[0]["id"] == "BRBD-4997"
    assert out[0]["mergedIds"] == ["BRBD-5187", "BRBD-5203"]


def test_a_shared_short_name_with_different_destinations_is_not_a_duplicate():
    """Boston: thirty-eight 'Red Line Shuttle' rows going to different places.

    This is the test that stops the feature from being harmful. Merging on the short name alone
    would tell a rider that one shuttle serves Broadway, Ashmont and Quincy Center."""
    out = merge_duplicate_routes([
        _r("Shuttle-BroadwayJFK", "Red Line Shuttle", "JFK/UMass - Broadway"),
        _r("Shuttle-AshmontJFK", "Red Line Shuttle", "Ashmont - JFK/UMass"),
        _r("Shuttle-BroadwayQuincy", "Red Line Shuttle", "Quincy Center - Broadway"),
    ])
    assert len(out) == 3
    assert all("mergedIds" not in r for r in out)


def test_the_same_number_on_two_components_stays_two_routes():
    """A feeder and a trunk sharing a number are not the same service, and the app already draws
    them in different colours — merging across that would make the colour a lie."""
    out = merge_duplicate_routes([
        _r("a", "9-3", "Portal Sur", component="feeder"),
        _r("b", "9-3", "Portal Sur", component="trunk"),
    ])
    assert len(out) == 2


def test_a_different_mode_is_a_different_route():
    out = merge_duplicate_routes([
        _r("a", "1", "Centro", mode="BUS"),
        _r("b", "1", "Centro", mode="TRAM"),
    ])
    assert len(out) == 2


def test_order_is_preserved_so_a_list_does_not_reshuffle():
    out = merge_duplicate_routes([
        _r("z1", "Z", "Last"),
        _r("a1", "A", "First"),
        _r("z2", "Z", "Last"),
    ])
    assert [r["shortName"] for r in out] == ["Z", "A"]


def test_nameless_routes_are_never_merged():
    """With nothing to compare, collapsing on component alone would fold a whole network into one
    row. They keep their place at the end instead."""
    out = merge_duplicate_routes([
        _r("a", "", ""),
        _r("b", "", ""),
        _r("c", "12", "Centro"),
    ])
    assert len(out) == 3
    assert out[0]["shortName"] == "12"


def test_whitespace_does_not_make_two_routes():
    out = merge_duplicate_routes([_r("a", "12 ", "Centro"), _r("b", "12", " Centro")])
    assert len(out) == 1


def test_an_already_clean_city_is_untouched():
    """Roma, Santiago, Toronto and Kuala Lumpur have no duplicates; this must cost them nothing."""
    rows = [_r(f"r{i}", str(i), f"Linea {i}") for i in range(5)]
    out = merge_duplicate_routes(rows)
    assert out == rows
