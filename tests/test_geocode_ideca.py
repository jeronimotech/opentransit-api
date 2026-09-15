"""Bogotá addresses resolve through the city's own cadastral geocoder (IDECA), and a name search is no
longer lost to a stop that shares one word with it.

Measured before this (58 real queries against production): 0 of 16 addresses correct — "Cra 7 # 72-41"
returned the stop "Cra. 43 B", every intersection a random station — and "Clínica Shaio" returned the
stop "Clínica del Niño" above the hospital Photon had found.
"""
import json

import httpx
import pytest

from app.admin_config import effective_city, mask_secrets, unmask_geocoder_patch, yaml_sections
from app.cities import City
from app.geocode import (
    ideca_result,
    looks_like_intersection,
    normalize_bogota_address,
    pretty_bogota_address,
    rank_results,
    search_ideca,
)
from app.ondemand import MASK

ALIASES = {"avenida boyacá": "AK 72", "av boyacá": "AK 72", "autopista norte": "AK 45", "calle 80": "AC 80",
           "nqs": "AK 30"}


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize("q", ["Calle 127 con Carrera 7", "Avenida Boyacá con Calle 80", "Cra 30 con Calle 45",
                               "Autopista Norte con 170", "Kr 7 esquina Cl 72"])
def test_intersections_are_recognised(q):
    assert looks_like_intersection(q)


@pytest.mark.parametrize("q", ["Museo del Oro", "Calle 85", "Portal Sur", "Clínica Shaio con urgencias"])
def test_names_are_not_intersections(q):
    assert not looks_like_intersection(q)


@pytest.mark.parametrize("q, want", [
    ("Carrera 15 No. 93-60", "KR 15 # 93-60"),
    ("Cra 10 # 15-22 Sur", "KR 10 # 15-22 SUR"),
    ("Cll 170 # 50-10", "CL 170 # 50-10"),
    ("Calle 127 con Carrera 7", "CL 127 # KR 7"),
    ("Avenida Boyacá con Calle 80", "AK 72 # AC 80"),
    ("Autopista Norte con 170", "AK 45 # 170"),
    ("Diagonal 40A # 14 - 05", "DG 40A # 14-05"),
    ("Av. Carrera 68 # 24-10", "AK 68 # 24-10"),
    ("Museo del Oro", "Museo del Oro"),
])
def test_bogota_nomenclature_is_normalised_for_the_geocoder(q, want):
    assert normalize_bogota_address(q, ALIASES) == want


@pytest.mark.parametrize("dirtrad, want", [
    ("KR 10 15 22 S", "Carrera 10 # 15-22 Sur"),
    ("CL 85 12 30", "Calle 85 # 12-30"),
    ("AC 127 7 42", "Avenida Calle 127 # 7-42"),
    ("DG 40A 14 05", "Diagonal 40A # 14-05"),
    ("KR 54 57B 20", "Carrera 54 # 57B-20"),
    ("CL 63 S 24 17", "Calle 63 # 24-17 Sur"),
])
def test_catastro_form_reads_like_a_person_writes_it(dirtrad, want):
    assert pretty_bogota_address(dirtrad) == want


# ------------------------------------------------------------------ the provider

IDECA_OK = {"response": {"success": True, "data": {
    "estado": "success", "tipo_direccion": "Asignada por Catastro", "dirtrad": "KR 10 15 22 S",
    "diraprox": "KR 10 15 22 S", "latitude": "4.57899525200003", "longitude": "-74.091970836",
    "xinput": -74.091970836, "yinput": 4.57899525200003, "nomseccat": "SOCIEGO", "localidad": "SAN CRISTOBAL",
    "codloc": "04", "lotcodigo": "004108030012", "nomupz": "SOSIEGO"}}}


def test_a_geocoder_answer_becomes_an_address_result_with_where_it_is():
    r = ideca_result(IDECA_OK["response"]["data"])
    assert r["type"] == "address" and r["source"] == "ideca"
    assert r["name"] == "Carrera 10 # 15-22 Sur"
    assert r["label"] == "Sociego · San Cristobal"
    assert (round(r["lat"], 5), round(r["lon"], 5)) == (4.579, -74.09197)


def test_an_approximate_answer_says_so():
    d = {**IDECA_OK["response"]["data"], "tipo_direccion": "Dirección por aproximación", "dirtrad": "AC 127 7 20"}
    r = ideca_result(d)
    assert r["name"] == "Avenida Calle 127 # 7-20" and r["label"].endswith("· aprox.")


def _city(bogota: City, **ideca) -> City:
    return effective_city(bogota, {"geocoder": {"ideca": {"enabled": True, "apiKey": "k-1234-5678", **ideca}}})


@pytest.mark.anyio
async def test_the_geocoder_is_asked_only_for_addresses_and_sends_the_normalised_query(bogota: City):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(dict(req.url.params))
        return httpx.Response(200, json=IDECA_OK)

    city = _city(bogota, aliases=ALIASES)
    out = await search_ideca(city, "Cra 10 # 15-22 Sur", transport=httpx.MockTransport(handler))
    assert [r["name"] for r in out] == ["Carrera 10 # 15-22 Sur"]
    assert calls == [{"cmd": "geocodificar", "apikey": "k-1234-5678", "query": "KR 10 # 15-22 SUR"}]

    await search_ideca(city, "Avenida Boyacá con Calle 80", transport=httpx.MockTransport(handler))
    assert calls[-1]["query"] == "AK 72 # AC 80"

    # a name is never sent: Photon and the stops own those
    assert await search_ideca(city, "Clínica Shaio", transport=httpx.MockTransport(handler)) == []
    assert len(calls) == 2


@pytest.mark.anyio
async def test_no_key_no_call_and_failures_degrade_silently(bogota: City):
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert bogota.geocoder.ideca.active is False or bogota.geocoder.ideca.api_key
    off = effective_city(bogota, {"geocoder": {"ideca": {"enabled": False}}})
    assert await search_ideca(off, "Cra 7 # 72-41", transport=httpx.MockTransport(boom)) == []
    on = _city(bogota)
    assert await search_ideca(on, "Cra 7 # 72-41", transport=httpx.MockTransport(boom)) == []
    miss = httpx.MockTransport(lambda req: httpx.Response(200, json={"response": {"success": False}}))
    assert await search_ideca(on, "Cra 7 # 72-41", transport=miss) == []


# ------------------------------------------------------------------ ranking


def _stop(name, typ="stop", n=3):
    return {"name": name, "type": typ, "source": "gtfs", "_nRoutes": n, "lat": 4.65, "lon": -74.08}


def _poi(name, typ="poi", source="photon"):
    return {"name": name, "type": typ, "source": source, "lat": 4.66, "lon": -74.07}


def test_the_cadastral_address_outranks_everything_for_an_address_query():
    rs = [_stop("Cra. 43 B - 4"), _poi("Carrera 7", typ="street"), _poi("Carrera 7 # 72-41", typ="address", source="ideca")]
    out = rank_results(rs, "Cra 7 # 72-41")
    assert [r["source"] for r in out] == ["ideca", "photon", "gtfs"]


def test_an_intersection_is_an_address_query_too():
    rs = [_stop("Calle 76", typ="station"), _poi("Avenida Calle 127 # 7-20", typ="address", source="ideca")]
    assert rank_results(rs, "Calle 127 con Carrera 7")[0]["source"] == "ideca"


@pytest.mark.parametrize("q, stop, poi", [
    ("Clínica Shaio", "Clínica del Niño", "Fundación Clínica Shaio"),
    ("Hospital San Ignacio", "Hospital", "Hospital Universitario San Ignacio"),
    ("Biblioteca Virgilio Barco", "Biblioteca", "Biblioteca Pública Virgilio Barco"),
    ("Centro Mayor", "Centro Memoria", "Centro Comercial Centro Mayor"),
    ("Gran Estación", "Estación San Bernardo", "Gran Estación"),
    ("Estadio El Campín", "Coliseo El Campín", "Estadio El Campín"),
])
def test_a_place_that_covers_the_whole_name_beats_a_stop_that_shares_a_word(q, stop, poi):
    rs = [_stop(stop, typ="station"), _stop(stop + " Norte"), _poi(poi)]
    assert rank_results(rs, q)[0]["name"] == poi


def test_a_stop_that_covers_the_whole_name_still_beats_the_place():
    rs = [_poi("Parque Simón Bolívar", typ="place"), _stop("Parque Simón Bolívar")]
    assert rank_results(rs, "Parque Simón Bolívar")[0]["source"] == "gtfs"
    rs = [_poi("Terminal de Transporte Salitre"), _stop("Terminal de Transporte")]
    assert rank_results(rs, "Terminal de Transporte")[0]["source"] == "gtfs"


def test_a_nearby_stop_wins_only_when_it_is_what_was_asked_for():
    here = (4.65, -74.08)
    shaio = {**_poi("Fundación Clínica Shaio"), "lat": 4.69, "lon": -74.05}
    rs = [_stop("Clínica del Niño"), shaio]
    assert rank_results(rs, "Clínica Shaio", *here)[0]["name"] == "Fundación Clínica Shaio"
    rs = [_stop("Clínica del Niño"), {**_poi("Clínica del Niño"), "lat": 4.69, "lon": -74.05}]
    assert rank_results(rs, "Clínica del Niño", *here)[0]["source"] == "gtfs"


def test_a_one_word_neighbourhood_prefers_the_place_named_so_over_a_stop_named_like_it():
    rs = [_stop("Br. Chicó Norte II Sector"), _poi("Chicó", typ="place")]
    assert rank_results(rs, "Chicó")[0]["type"] == "place"
    # ...but a station still leads a one-word category search
    rs = [_stop("Portal Norte", typ="station"), _poi("Portal", typ="place"), _stop("Portal", n=1)]
    assert rank_results(rs, "portal")[0]["name"] == "Portal Norte"


# ------------------------------------------------------------------ admin: the key


def test_admin_sections_carry_the_geocoder_and_the_key_is_masked(bogota: City):
    city = _city(bogota)
    secs = yaml_sections(city)
    assert secs["geocoder"]["ideca"]["enabled"] is True
    masked = mask_secrets(secs)
    shown = masked["geocoder"]["ideca"]["apiKey"]
    assert shown.startswith(MASK) and shown.endswith("5678") and "k-1234-5678" not in json.dumps(masked)
    # the mask echoed back keeps the stored key; a real value is stored as sent; omitting keeps it
    assert unmask_geocoder_patch({"ideca": {"apiKey": shown, "enabled": False}}, city, bogota) == \
        {"ideca": {"apiKey": "k-1234-5678", "enabled": False}}
    assert unmask_geocoder_patch({"ideca": {"apiKey": "new-key"}}, city, bogota) == {"ideca": {"apiKey": "new-key"}}
    assert unmask_geocoder_patch({"ideca": {"enabled": True}}, city, bogota) == {"ideca": {"enabled": True}}


def test_the_public_city_never_carries_the_geocoder_key(bogota: City):
    city = _city(bogota)
    assert "k-1234-5678" not in json.dumps(city.public())


def test_a_station_that_shares_no_word_does_not_lead_a_one_word_search():
    # "La Castellana" came back for "castilla" by trigram similarity and, being a station, led the page
    rs = [_stop("La Castellana", typ="station"), _poi("Castilla", typ="place")]
    assert rank_results(rs, "Castilla")[0]["name"] == "Castilla"


def test_an_exact_place_far_from_the_city_is_a_namesake_not_the_answer():
    faca = {**_poi("Chicó", typ="place"), "lat": 4.81, "lon": -74.35}       # Facatativá, 35 km west
    rs = [_stop("Br. Chicó Norte II Sector"), faca]
    out = rank_results(rs, "Chicó", city_center=(4.6534, -74.0836))
    assert out[0]["source"] == "gtfs"
    here = {**_poi("Chicó", typ="place"), "lat": 4.67, "lon": -74.05}
    assert rank_results([_stop("Br. Chicó Norte II Sector"), here], "Chicó", city_center=(4.6534, -74.0836))[0]["type"] == "place"
