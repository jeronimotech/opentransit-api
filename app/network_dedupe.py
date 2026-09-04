"""
Server-side dedupe of network shapes.

Feeds like Bogotá's model one commercial route as many GTFS routes (direction and peak-only variants),
each with its own shape, so `/network` used to ship ~1,050 lines where ~half are the same street twice.
Rules, per group (component + route short name, falling back to the route id):
  1. collapse exact duplicates (same encoded polyline),
  2. keep the longest shape,
  3. keep any other shape whose simplified points are NOT >= `coverage` covered (within `tol_m`)
     by the shapes already kept in the group.
Everything else is marked non-canonical and points at the shape that stands for it.
Pure function; ingest persists the result and `/network` serves canonical rows only.
"""
from dataclasses import dataclass, field

from .geo import coverage_fraction, decode_polyline, polyline_length_m


@dataclass
class ShapeIn:
    shape_id: str
    route_id: str | None
    group_key: str
    encoded: str
    direction_id: int | None = None


@dataclass
class ShapeOut:
    shape_id: str
    is_canonical: bool
    canonical_shape_id: str | None
    length_m: int
    covered: float = 0.0
    represents: list[str] = field(default_factory=list)   # route ids collapsed into this shape


def dedupe_shapes(shapes: list[ShapeIn], coverage: float = 0.9, tol_m: float = 30.0) -> dict[str, ShapeOut]:
    groups: dict[str, list[ShapeIn]] = {}
    for s in shapes:
        groups.setdefault(s.group_key, []).append(s)
    out: dict[str, ShapeOut] = {}
    for members in groups.values():
        decoded = {m.shape_id: decode_polyline(m.encoded) for m in members}
        length = {sid: polyline_length_m(pts) for sid, pts in decoded.items()}
        members.sort(key=lambda m: (-length[m.shape_id], m.shape_id))
        kept: list[ShapeIn] = []
        by_encoded: dict[str, str] = {}
        for m in members:
            pts = decoded[m.shape_id]
            twin = by_encoded.get(m.encoded)
            if twin is not None:
                out[m.shape_id] = ShapeOut(m.shape_id, False, twin, int(length[m.shape_id]), 1.0)
                out[twin].represents.append(m.route_id or m.shape_id)
                continue
            cov = coverage_fraction(pts, [decoded[k.shape_id] for k in kept], tol_m) if kept else 0.0
            if kept and cov >= coverage:
                # attribute it to the kept shape that covers it best
                best = max(kept, key=lambda k: coverage_fraction(pts, [decoded[k.shape_id]], tol_m))
                out[m.shape_id] = ShapeOut(m.shape_id, False, best.shape_id, int(length[m.shape_id]), cov)
                out[best.shape_id].represents.append(m.route_id or m.shape_id)
                continue
            kept.append(m)
            by_encoded[m.encoded] = m.shape_id
            out[m.shape_id] = ShapeOut(m.shape_id, True, None, int(length[m.shape_id]), cov,
                                       [m.route_id or m.shape_id])
    return out
