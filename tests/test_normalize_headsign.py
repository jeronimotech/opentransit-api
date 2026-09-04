from app.normalize import clean_headsign


def test_headsign_equal_to_route_is_dropped():
    assert clean_headsign("G12", "G12") is None
    assert clean_headsign(" g12 ", "G12") is None


def test_real_headsign_is_kept():
    assert clean_headsign("Portal Sur", "G12") == "Portal Sur"
    assert clean_headsign("Portal Sur", None) == "Portal Sur"


def test_empty_headsign_is_none():
    assert clean_headsign("", "G12") is None
    assert clean_headsign(None, None) is None
