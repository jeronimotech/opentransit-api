"""The walking limit as a limit.

Asked for by TransMilenio against 1.16.0 (1.11): the setting lowered OTP's walking reluctance, so an
option that walked further than the rider allowed still came back.
"""
from app.walk_limit import apply_walk_limit, walk_limit_warning


def _it(walk: float | None, name: str = "x") -> dict:
    return {"id": name} | ({} if walk is None else {"walkDistanceMeters": walk})


def test_an_option_within_the_limit_is_kept():
    kept, dropped = apply_walk_limit([_it(400)], 800)
    assert [i["id"] for i in kept] == ["x"]
    assert dropped == 0


def test_an_option_over_the_limit_is_dropped():
    kept, dropped = apply_walk_limit([_it(900)], 800)
    assert kept == []
    assert dropped == 1


def test_exactly_the_limit_is_within_it():
    """A rider who said 800 m can walk 800 m."""
    kept, _ = apply_walk_limit([_it(800)], 800)
    assert len(kept) == 1


def test_order_is_preserved():
    kept, dropped = apply_walk_limit([_it(100, "a"), _it(2000, "b"), _it(300, "c")], 800)
    assert [i["id"] for i in kept] == ["a", "c"]
    assert dropped == 1


def test_an_option_with_no_walk_figure_is_kept():
    """Absent is not the same as over: dropping it would hide a metro-only trip because the field
    was missing."""
    kept, dropped = apply_walk_limit([_it(None)], 100)
    assert len(kept) == 1
    assert dropped == 0


def test_the_response_says_what_was_dropped():
    """A filter that silently empties a list is indistinguishable from "no service", which is the
    failure this feature exists to prevent."""
    w = walk_limit_warning(2, 5, 800)
    assert len(w) == 1
    assert w[0].startswith("WALK_LIMIT_FILTERED:")
    assert "2 of 5" in w[0]
    assert "800 m" in w[0]


def test_nothing_dropped_says_nothing():
    assert walk_limit_warning(0, 5, 800) == []
