"""POI bbox/type filtering and the per-connection SSE stream filter."""
from app.routers.pois import filter_pois
from app.routers.vehicles import StreamFilter


def _f(t: str, lon: float, lat: float) -> dict:
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": {"type": t}}


def test_poi_filters():
    feats = [_f("atm", -74.08, 4.65), _f("toilets", -74.08, 4.65), _f("atm", -74.20, 4.65)]
    assert len(filter_pois(feats, None, None)) == 3
    assert len(filter_pois(feats, (-74.10, 4.60, -74.05, 4.70), None)) == 2
    assert [f["properties"]["type"] for f in filter_pois(feats, (-74.10, 4.60, -74.05, 4.70), {"atm"})] == ["atm"]
    assert filter_pois(feats, None, {"library"}) == []


def _v(i: str, lon: float, lat: float, route: str = "bogota:G12") -> dict:
    return {"id": i, "lon": lon, "lat": lat, "routeId": route}


def test_stream_filter_bbox_emits_removed_when_vehicle_leaves():
    flt = StreamFilter((-74.10, 4.60, -74.05, 4.70), None)
    full = flt.full({"type": "full", "count": 3, "vehicles": [_v("a", -74.08, 4.65), _v("b", -74.20, 4.65),
                                                             _v("c", -74.06, 4.66)]})
    assert [v["id"] for v in full["vehicles"]] == ["a", "c"] and full["count"] == 2
    delta = flt.delta({"type": "delta", "updated": [_v("a", -74.30, 4.65), _v("c", -74.07, 4.66),
                                                    _v("d", -74.07, 4.61)], "removed": ["b"]})
    assert [v["id"] for v in delta["updated"]] == ["c", "d"]        # a left the bbox, b was never sent
    assert delta["removed"] == ["a"] and delta["count"] == 2       # a is reported as gone for this client


def test_stream_filter_route_ids_and_passthrough():
    flt = StreamFilter(None, {"bogota:G12"})
    full = flt.full({"type": "full", "count": 2, "vehicles": [_v("a", 0, 0), _v("b", 0, 0, "bogota:B13")]})
    assert [v["id"] for v in full["vehicles"]] == ["a"]
    none = StreamFilter(None, None)
    frame = {"type": "delta", "updated": [_v("x", 0, 0)], "removed": ["y"], "count": 1}
    assert none.delta(frame) is frame
