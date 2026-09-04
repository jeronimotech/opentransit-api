from app.geocode import rank_results


def _r(name, typ="stop", source="gtfs", n=0):
    return {"name": name, "type": typ, "source": source, "_nRoutes": n, "lat": 0, "lon": 0}


def test_stations_then_exact_then_prefix_then_busier():
    rs = [
        _r("Calle 26 - Portal", n=2),
        _r("Portal Norte", typ="station"),
        _r("Portal de la 80", n=9),
        _r("Portal", n=1),
        _r("Portal Suba Bakery", typ="poi", source="photon"),
        _r("Portal de la 80", n=3),
    ]
    out = [r["name"] for r in rank_results(rs, "portal")]
    assert out[0] == "Portal Norte"                     # station wins
    assert out[1] == "Portal"                           # exact
    assert out[2] == "Portal de la 80" and rs[2] is rank_results(rs, "portal")[2]   # prefix, busier first
    assert out[-1] == "Portal Suba Bakery"              # photon last


def test_accent_insensitive():
    rs = [_r("Estación Ricaurte"), _r("Ricaurte Norte")]
    out = [r["name"] for r in rank_results(rs, "estacion ric")]
    assert out[0] == "Estación Ricaurte"
