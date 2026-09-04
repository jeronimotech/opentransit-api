"""Server-side dedupe of network shapes (exact duplicates + coverage-based variants)."""
from app.geo import coverage_fraction, encode_polyline, polyline_length_m
from app.network_dedupe import ShapeIn, dedupe_shapes

# A straight corridor along Bogotá's Autopista Norte, ~4 km, plus variants.
BASE = [(-74.0450, 4.7000 + i * 0.002) for i in range(20)]
SHORT = BASE[:12]                                  # first ~60 % of the corridor (fully covered)
SHIFTED = [(x + 0.0001, y) for x, y in BASE]      # ~11 m east: same street, other carriageway
BRANCH = BASE[:10] + [(-74.0450 - i * 0.002, 4.7180) for i in range(1, 12)]   # turns west: new geometry


def _s(sid, enc_pts, route="R", group="dual|10-1|None", direction=None):
    return ShapeIn(sid, route, group, encode_polyline(enc_pts), direction)


def test_length_and_coverage_helpers():
    assert 3900 < polyline_length_m(BASE) < 4300
    assert coverage_fraction(SHORT, [BASE]) == 1.0
    assert coverage_fraction(SHIFTED, [BASE], tol_m=30) == 1.0
    assert coverage_fraction(SHIFTED, [BASE], tol_m=5) == 0.0
    assert coverage_fraction(BRANCH, [BASE]) < 0.6
    assert coverage_fraction([], [BASE]) == 0.0 and coverage_fraction(BASE, []) == 0.0


def test_exact_duplicates_collapse_to_one():
    out = dedupe_shapes([_s("a", BASE, "R1"), _s("b", BASE, "R2")])
    assert out["a"].is_canonical and not out["b"].is_canonical
    assert out["b"].canonical_shape_id == "a" and out["b"].covered == 1.0
    assert out["a"].represents == ["R1", "R2"]


def test_longest_kept_and_covered_variants_dropped():
    out = dedupe_shapes([_s("short", SHORT, "R2"), _s("long", BASE, "R1"), _s("shift", SHIFTED, "R3")])
    assert out["long"].is_canonical
    assert not out["short"].is_canonical and out["short"].canonical_shape_id == "long"
    assert not out["shift"].is_canonical and out["shift"].canonical_shape_id == "long"
    assert sorted(out["long"].represents) == ["R1", "R2", "R3"]


def test_branch_is_kept_as_second_canonical():
    out = dedupe_shapes([_s("base", BASE, "R1"), _s("branch", BRANCH, "R2")])
    assert out["base"].is_canonical and out["branch"].is_canonical
    assert out["branch"].represents == ["R2"]


def test_groups_never_mix():
    out = dedupe_shapes([_s("a", BASE, "R1", group="trunk|G12|None"), _s("b", BASE, "R2", group="dual|G12|None")])
    assert out["a"].is_canonical and out["b"].is_canonical


def test_coverage_threshold_is_configurable():
    # BRANCH is slightly longer so it is kept first; with a 40 % threshold BASE (50 % covered) is collapsed.
    out = dedupe_shapes([_s("base", BASE), _s("branch", BRANCH)], coverage=0.4)
    assert out["branch"].is_canonical and not out["base"].is_canonical
    assert out["base"].canonical_shape_id == "branch"
