from app.geocode import rank_results


def _r(name, lat, lon, kind="stop", source="gtfs", n=1):
    return {"name": name, "lat": lat, "lon": lon, "type": kind, "source": source, "_nRoutes": n}


def test_nearby_stops_rank_first_when_position_given():
    rs = [_r("Portal Norte", 4.7546, -74.0459, "station", n=40),
          _r("Calle 26 - Portal", 4.6534, -74.0836, n=3),          # ~5 m from the user
          _r("Portal Sur", 4.5978, -74.1616, "station", n=30),
          _r("Portal del Rosal", 4.6540, -74.0840, "poi", "photon")]
    out = rank_results(rs, "portal", 4.6534, -74.0836)
    assert [r["name"] for r in out][:2] == ["Calle 26 - Portal", "Portal Norte"]
    assert out[0]["distanceMeters"] is not None and out[0]["distanceMeters"] < 50
    assert out[-1]["source"] == "photon"


def test_without_position_stations_first():
    rs = [_r("Calle 26 - Portal", 4.6534, -74.0836), _r("Portal Norte", 4.7546, -74.0459, "station")]
    out = rank_results(rs, "portal")
    assert out[0]["name"] == "Portal Norte" and out[0]["distanceMeters"] is None
