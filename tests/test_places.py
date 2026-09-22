"""v2.2 — the IDECA answer cache and the city's named areas (barrios, localidades) from its cadastre."""
import datetime as dt
import json

import httpx
import pytest

from app import geocode as geo
from app.admin_config import effective_city, yaml_sections
from app.cities import City, PlaceAreas
from app.geocode import rank_results, search_ideca
from app.places import (
    MemoryGeocodeCache,
    MemoryPlaceAreaStore,
    area_result,
    areas_from_features,
    cache_fresh,
    fetch_arcgis_layer,
    refresh_place_areas,
    search_areas,
)

IDECA_OK = {"response": {"success": True, "data": {
    "estado": "success", "tipo_direccion": "Asignada por Catastro", "dirtrad": "KR 7 72 41",
    "latitude": "4.65566922199997", "longitude": "-74.055227881", "nomseccat": "PORCIUNCULA",
    "localidad": "CHAPINERO"}}}


def _city(bogota: City, **ideca) -> City:
    return effective_city(bogota, {"geocoder": {"ideca": {"enabled": True, "apiKey": "k-1234-5678", **ideca}}})


# ------------------------------------------------------------------ cache


@pytest.fixture
def cache(monkeypatch):
    c = MemoryGeocodeCache()
    monkeypatch.setattr(geo, "cache", c)
    return c


@pytest.mark.anyio
async def test_a_repeated_address_is_answered_from_the_cache_without_a_call(bogota: City, cache):
    calls = []

    def handler(req):
        calls.append(req.url.params["query"])
        return httpx.Response(200, json=IDECA_OK)

    city = _city(bogota)
    t = httpx.MockTransport(handler)
    first = await search_ideca(city, "Cra 7 # 72-41", transport=t)
    again = await search_ideca(city, "carrera 7 No 72-41", transport=t)     # same door, typed differently
    assert first == again and first[0]["name"] == "Carrera 7 # 72-41"
    assert calls == ["KR 7 # 72-41"]                                       # one upstream call, not two
    assert (await cache.stats("bogota")) == {"rows": 1, "hits": 1}


@pytest.mark.anyio
async def test_a_miss_is_remembered_for_a_week_only(bogota: City, cache):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"response": {"success": False}})

    city = _city(bogota)
    t = httpx.MockTransport(handler)
    assert await search_ideca(city, "Kr 999 # 1-1", transport=t) == []
    assert await search_ideca(city, "Kr 999 # 1-1", transport=t) == []
    assert len(calls) == 1
    # eight days later the miss is asked again; a hit would not be
    key = ("bogota", "kr 999 # 1-1")
    res, at, hits = cache.rows[key]
    cache.rows[key] = (res, at - dt.timedelta(days=8), hits)
    assert await search_ideca(city, "Kr 999 # 1-1", transport=t) == []
    assert len(calls) == 2


def test_cache_freshness_rules():
    assert cache_fresh(True, {"x": 1}, dt.timedelta(days=100))
    assert not cache_fresh(True, {"x": 1}, dt.timedelta(days=200))
    assert cache_fresh(True, None, dt.timedelta(days=6))
    assert not cache_fresh(True, None, dt.timedelta(days=8))
    assert not cache_fresh(False, None, None)


@pytest.mark.anyio
async def test_when_the_upstream_fails_a_stale_hit_still_answers(bogota: City, cache):
    city = _city(bogota)
    ok = httpx.MockTransport(lambda req: httpx.Response(200, json=IDECA_OK))
    assert await search_ideca(city, "Cra 7 # 72-41", transport=ok)
    key = ("bogota", "kr 7 # 72-41")
    res, at, hits = cache.rows[key]
    cache.rows[key] = (res, at - dt.timedelta(days=400), hits)               # long past the TTL

    def boom(req):
        raise httpx.ConnectError("down")

    out = await search_ideca(city, "Cra 7 # 72-41", transport=httpx.MockTransport(boom))
    assert out and out[0]["name"] == "Carrera 7 # 72-41"                    # stale beats nothing


# ------------------------------------------------------------------ areas

SQ = lambda lon0, lat0, d: [[[lon0, lat0], [lon0 + d, lat0], [lon0 + d, lat0 + d], [lon0, lat0 + d], [lon0, lat0]]]  # noqa: E731

BARRIOS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"SCACODIGO": "008101", "SCANOMBRE": "CHICO NORTE", "OBJECTID": 1},
     "geometry": {"type": "Polygon", "coordinates": SQ(-74.06, 4.67, 0.01)}},
    {"type": "Feature", "properties": {"SCACODIGO": "008102", "SCANOMBRE": "CHICO", "OBJECTID": 2},
     "geometry": {"type": "Polygon", "coordinates": SQ(-74.06, 4.66, 0.01)}},
    {"type": "Feature", "properties": {"SCACODIGO": "004622", "SCANOMBRE": "BRASIL", "OBJECTID": 3},
     "geometry": {"type": "MultiPolygon", "coordinates": [SQ(-74.19, 4.62, 0.01)]}},
    {"type": "Feature", "properties": {"SCACODIGO": "x", "SCANOMBRE": "", "OBJECTID": 4},
     "geometry": {"type": "Polygon", "coordinates": SQ(-74.1, 4.6, 0.01)}},
]}
LOCALIDADES = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"LOCCODIGO": "02", "LOCNOMBRE": "CHAPINERO"},
     "geometry": {"type": "Polygon", "coordinates": SQ(-74.08, 4.64, 0.06)}},
    {"type": "Feature", "properties": {"LOCCODIGO": "08", "LOCNOMBRE": "KENNEDY"},
     "geometry": {"type": "Polygon", "coordinates": SQ(-74.2, 4.6, 0.05)}},
]}


def test_features_become_areas_with_a_readable_name_and_a_centre():
    areas = areas_from_features(BARRIOS["features"], name_field="SCANOMBRE", code_field="SCACODIGO")
    assert [a["name"] for a in areas] == ["Chico Norte", "Chico", "Brasil"]        # the nameless one dropped
    assert areas[1]["name_norm"] == "chico" and areas[1]["code"] == "008102"
    assert (round(areas[1]["lat"], 3), round(areas[1]["lon"], 3)) == (4.664, -74.056)


@pytest.mark.anyio
async def test_arcgis_layers_are_read_page_by_page():
    pages = []

    def handler(req):
        off = int(req.url.params["resultOffset"])
        pages.append(off)
        feats = BARRIOS["features"]
        chunk = feats[off:off + 2]
        return httpx.Response(200, json={"type": "FeatureCollection", "features": chunk,
                                         "exceededTransferLimit": off + 2 < len(feats)})

    feats = await fetch_arcgis_layer("https://gis.example/MapServer/37", transport=httpx.MockTransport(handler), page=2)
    assert len(feats) == 4 and pages == [0, 2]


@pytest.mark.anyio
async def test_a_plain_geojson_url_is_read_in_one_go():
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json=BARRIOS)

    feats = await fetch_arcgis_layer("https://github.com/x/releases/download/places/bogota-barrios.geojson",
                                     transport=httpx.MockTransport(handler))
    assert len(feats) == 4 and calls == ["https://github.com/x/releases/download/places/bogota-barrios.geojson"]


@pytest.mark.anyio
async def test_refresh_mirrors_both_layers_and_names_each_barrio_its_localidad(bogota: City):
    def handler(req):
        body = LOCALIDADES if req.url.path.endswith("/48/query") else BARRIOS
        return httpx.Response(200, json=body)

    store = MemoryPlaceAreaStore()
    cfg = PlaceAreas(enabled=True, barrios_url="https://gis.example/MapServer/37",
                     localidades_url="https://gis.example/MapServer/48")
    result = await refresh_place_areas(store, bogota, cfg, transport=httpx.MockTransport(handler))
    assert result == {"localidades": 2, "barrios": 3, "withParent": 3}
    city = effective_city(bogota, {"geocoder": {"areas": cfg.admin()}})
    out = await search_areas(store, city, "chicó", 5, "es")
    assert [r["name"] for r in out] == ["Chico", "Chico Norte"]
    assert out[0]["label"] == "Barrio · Chapinero" and out[0]["source"] == "catastro" and out[0]["type"] == "place"
    assert (await search_areas(store, city, "kenn", 5, "en"))[0]["label"] == "District"
    assert (await store.stats("bogota"))["barrios"] == 3


def test_area_result_is_localised():
    row = {"kind": "localidad", "code": "02", "name": "Chapinero", "lat": 4.65, "lon": -74.06, "parent_name": None}
    assert area_result(row, "en")["label"] == "District"
    assert area_result(row, "es")["id"] == "area:localidad:02"


def test_the_neighbourhood_beats_the_stop_named_like_it_and_the_far_photon_namesake():
    stop = {"name": "Br. Chicó Norte II Sector", "type": "stop", "source": "gtfs", "_nRoutes": 3,
            "lat": 4.67, "lon": -74.05}
    faca = {"name": "Chicó", "type": "place", "source": "photon", "lat": 4.81, "lon": -74.35}
    barrio = {"name": "Chico", "type": "place", "source": "catastro", "lat": 4.664, "lon": -74.056}
    out = rank_results([stop, faca, barrio], "Chicó", city_center=(4.6534, -74.0836))
    assert [r["source"] for r in out] == ["catastro", "gtfs", "photon"]


def test_admin_sections_carry_the_areas(bogota: City):
    secs = yaml_sections(bogota)
    assert set(secs["geocoder"]["areas"]) >= {"enabled", "barriosUrl", "localidadesUrl", "refreshDays"}
    assert "places-bogota/bogota-barrios.geojson" in json.dumps(secs["geocoder"]["areas"])


def test_a_rider_far_from_the_city_still_gets_its_neighbourhoods():
    """Seen from the app with the simulator in California: "Cedritos" listed five stops before the barrio,
    because the namesake rule measured from the user only."""
    stop = {"name": "Br. Cedritos del Sur II", "type": "stop", "source": "gtfs", "_nRoutes": 3,
            "lat": 4.57, "lon": -74.13}
    barrio = {"name": "Cedritos", "type": "place", "source": "catastro", "lat": 4.72, "lon": -74.03}
    out = rank_results([stop, barrio], "Cedritos", 37.77, -122.42, city_center=(4.6534, -74.0836))
    assert out[0]["source"] == "catastro"
    # ...while a namesake far from both the user and the city still is not the answer
    faca = {"name": "Chicó", "type": "place", "source": "photon", "lat": 4.81, "lon": -74.35}
    near = {"name": "Br. Chicó Norte", "type": "stop", "source": "gtfs", "_nRoutes": 3, "lat": 4.67, "lon": -74.05}
    assert rank_results([faca, near], "Chicó", 37.77, -122.42, city_center=(4.6534, -74.0836))[0]["source"] == "gtfs"
