"""v1.6 Open Mobility Foundation: CDS 1.1.0 curbs (paid parking first) and MDS 2.1.0 policy/geography."""
import datetime as dt
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admin_config import MemoryConfigStore, effective_city
from app.cities import City
from app.errors import install_error_handlers
from app.openmobility import (
    CDS_VERSION,
    MDS_VERSION,
    MEDIA_CDS,
    MemoryOpenMobilityStore,
    city_now,
    curb_public,
    distance_to_geometry_m,
    format_amount,
    ms,
    next_change,
    parse_curbs_document,
    parse_mds_documents,
    price_label,
    spans_active,
    time_span_active,
    zones_public,
)
from app.routers import admin, health, openmobility, platform
from app.rt import RTCache
from app.runtime import CityRuntime

pytestmark = pytest.mark.anyio

TOKEN = {"X-Admin-Token": "test-token"}
# Parque de la 93 kerb (a real block, made-up regulation): paid parking 07:00–20:00 Mon–Sat, free otherwise.
ZONE_93 = {
    "curb_zone_id": "3f5a9d84-2b1e-4c77-9c3f-8f6b1d0a55c1",
    "name": "Cra 13 con Cl 93 · costado oriental",
    "street_name": "Carrera 13",
    "geometry": {"type": "Polygon",
                 "coordinates": [[[-74.0487, 4.6763], [-74.0483, 4.6763], [-74.0483, 4.6769],
                                  [-74.0487, 4.6769], [-74.0487, 4.6763]]]},
    "curb_policy_ids": ["c0ffee00-1111-4222-8333-444455556666", "d1d1d1d1-2222-4333-8444-555566667777"],
    "available_spaces": 4,
    "available": True,
    "availability_time": 1757116800000,
}
PAID_PARKING = {
    "curb_policy_id": "c0ffee00-1111-4222-8333-444455556666",
    "name": "Parqueo pago",
    "priority": 2,
    "time_spans": [{"days_of_week": ["mon", "tue", "wed", "thu", "fri", "sat"],
                    "time_of_day_start": "07:00", "time_of_day_end": "20:00"}],
    "rules": [{"activity": "parking", "max_stay": 2, "max_stay_unit": "hour", "user_classes": ["car"],
               "rate": [{"rate": 4200, "rate_unit": "hour", "maximum_fee": 8400}]}],
}
# A higher-priority ban that wins during the evening peak (lower `priority` number wins in CDS).
NO_STOPPING_PEAK = {
    "curb_policy_id": "d1d1d1d1-2222-4333-8444-555566667777",
    "name": "Prohibido detenerse en hora pico",
    "priority": 1,
    "time_spans": [{"days_of_week": ["mon", "tue", "wed", "thu", "fri"],
                    "time_of_day_start": "17:00", "time_of_day_end": "20:00"}],
    "rules": [{"activity": "no stopping"}],
}
# Overnight loading bay near Calle 100 (wraps midnight).
ZONE_100 = {
    "curb_zone_id": "9e2b7c14-6d3a-4f58-b1c0-2a7d9e4f0b33",
    "name": "Cl 100 · bahía de cargue",
    "geometry": {"type": "LineString", "coordinates": [[-74.0530, 4.6845], [-74.0526, 4.6845]]},
    "curb_policy_ids": ["ba1e0000-8888-4999-8aaa-bbbbccccdddd"],
}
NIGHT_LOADING = {
    "curb_policy_id": "ba1e0000-8888-4999-8aaa-bbbbccccdddd",
    "name": "Cargue nocturno",
    "priority": 1,
    "time_spans": [{"time_of_day_start": "22:00", "time_of_day_end": "06:00"}],
    "rules": [{"activity": "loading", "user_classes": ["truck", "van", "delivery"]}],
}


def _city(bogota: City, **cds) -> City:
    patch = {"openMobility": {"cds": {"enabled": True, "publish": True, **cds},
                              "mds": {"enabled": True, "publishPolicy": True}}}
    return effective_city(bogota, patch)


def _app(city: City) -> tuple[FastAPI, CityRuntime, MemoryOpenMobilityStore]:
    app = FastAPI()
    install_error_handlers(app)
    for r in (platform, openmobility, admin, health):
        app.include_router(r.router)
    rt = CityRuntime(city=city, rt=RTCache(city), otp=None)  # type: ignore[arg-type]
    rt.base_city = city
    app.state.cities = {"bogota": rt}
    app.state.config_store = MemoryConfigStore()
    store = MemoryOpenMobilityStore()
    app.state.openmobility_store = store
    return app, rt, store


async def _seed(store: MemoryOpenMobilityStore, zones=None, policies=None) -> None:
    """Seed the way the admin endpoint does, so the stored documents carry the spec's required dates."""
    zones = zones if zones is not None else [ZONE_93, ZONE_100]
    policies = policies if policies is not None else [PAID_PARKING, NO_STOPPING_PEAK, NIGHT_LOADING]
    norm_zones, norm_policies = parse_curbs_document({"zones": zones, "policies": policies})
    await store.put_curbs("bogota", norm_zones, norm_policies, replace=True)


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ------------------------------------------------------------------ time spans
def _bog(y, m, d, hh, mm=0) -> dt.datetime:
    from zoneinfo import ZoneInfo
    return dt.datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/Bogota"))


def test_weekday_window_is_inclusive_start_exclusive_end():
    span = PAID_PARKING["time_spans"][0]
    assert time_span_active(span, _bog(2026, 9, 8, 7, 0), "CO") is True      # Tuesday 07:00, inclusive
    assert time_span_active(span, _bog(2026, 9, 8, 19, 59), "CO") is True
    assert time_span_active(span, _bog(2026, 9, 8, 20, 0), "CO") is False    # exclusive end
    assert time_span_active(span, _bog(2026, 9, 8, 6, 59), "CO") is False
    assert time_span_active(span, _bog(2026, 9, 13, 10, 0), "CO") is False   # Sunday is not listed


def test_overnight_window_wraps_midnight():
    span = NIGHT_LOADING["time_spans"][0]                                    # 22:00 -> 06:00
    assert time_span_active(span, _bog(2026, 9, 8, 23, 30), "CO") is True
    assert time_span_active(span, _bog(2026, 9, 9, 2, 0), "CO") is True
    assert time_span_active(span, _bog(2026, 9, 9, 6, 0), "CO") is False
    assert time_span_active(span, _bog(2026, 9, 9, 12, 0), "CO") is False


def test_designated_period_holidays_uses_the_city_calendar():
    span = {"designated_period": "holidays"}
    assert time_span_active(span, _bog(2026, 1, 1, 10, 0), "CO") is True      # Año Nuevo
    assert time_span_active(span, _bog(2026, 9, 8, 10, 0), "CO") is False
    inverted = {"designated_period": "holidays", "designated_period_except": True}
    assert time_span_active(inverted, _bog(2026, 1, 1, 10, 0), "CO") is False
    assert time_span_active(inverted, _bog(2026, 9, 8, 10, 0), "CO") is True


def test_empty_span_and_months_days_of_month():
    assert spans_active([], _bog(2026, 9, 8, 3, 0), "CO") is True
    assert time_span_active({"months": [12]}, _bog(2026, 9, 8, 3, 0), "CO") is False
    assert time_span_active({"days_of_month": [8]}, _bog(2026, 9, 8, 3, 0), "CO") is True


def test_next_change_finds_the_edge():
    span = PAID_PARKING["time_spans"]
    t = next_change(span, _bog(2026, 9, 8, 10, 0), "CO")                      # inside -> ends at 20:00
    assert t is not None and t.hour == 20 and t.day == 8
    t2 = next_change(span, _bog(2026, 9, 8, 21, 0), "CO")                     # outside -> starts 07:00 tomorrow
    assert t2 is not None and t2.hour == 7 and t2.day == 9


# ------------------------------------------------------------------ money (the CDS currency caveat)
def test_rate_amounts_respect_minor_units():
    assert format_amount(4200, "COP", 1, "es") == "$ 4.200"                   # Bogotá quotes whole pesos
    assert format_amount(250, "USD", 100, "en") == "$2,50"                    # cents elsewhere


def test_price_label_reads_like_a_sign(bogota: City):
    city = _city(bogota)
    label = price_label(PAID_PARKING["rules"], city)
    assert label == "$ 4.200 / hora (máx $ 8.400) · máx 2 hora"


def test_price_label_in_cents_currency(bogota: City):
    city = effective_city(bogota, {"openMobility": {"cds": {"enabled": True, "rateCurrency": "USD",
                                                            "rateMinorUnits": 100}}})
    rules = [{"activity": "parking", "rate": [{"rate": 250, "rate_unit": "hour"}]}]
    assert price_label(rules, city).startswith("$ 2,50 / hora")


# ------------------------------------------------------------------ priority, legality, nearby
def test_lower_priority_number_wins(bogota: City):
    city = _city(bogota)
    policies = {p["curb_policy_id"]: p for p in (PAID_PARKING, NO_STOPPING_PEAK)}
    midday = curb_public(ZONE_93, policies, _bog(2026, 9, 8, 12, 0), city, user_class="car")
    assert midday["allowed"] is True and "Estacionamiento" in midday["whyLegal"]
    assert "$ 4.200" in midday["whyLegal"] and midday["priceLabel"].startswith("$ 4.200")
    peak = curb_public(ZONE_93, policies, _bog(2026, 9, 8, 18, 0), city, user_class="car")
    assert peak["allowed"] is False and "Prohibido detenerse" in peak["whyLegal"]


def test_availability_fields_survive(bogota: City):
    city = _city(bogota)
    out = curb_public(ZONE_93, {PAID_PARKING["curb_policy_id"]: PAID_PARKING},
                      _bog(2026, 9, 8, 12, 0), city, user_class="car")
    assert out["availableSpaces"] == 4 and out["available"] is True
    assert out["availabilityTime"].startswith("2025-") or out["availabilityTime"].startswith("2026-")


async def test_nearby_ranks_legal_first_and_filters_user_class(bogota: City):
    app, rt, store = _app(_city(bogota))
    await _seed(store)
    async with _client(app) as c:
        r = await c.get("/v1/cities/bogota/curbs/nearby",
                        params={"lat": 4.6766, "lon": -74.0485, "radius": 3000, "userClass": "car",
                                "at": "2026-09-08T12:00:00-05:00"})
        assert r.status_code == 200
        body = r.json()
        # the loading bay is truck/van only, so a car request never sees it
        assert [c["name"] for c in body["curbs"]] == [ZONE_93["name"]]
        first = body["curbs"][0]
        assert first["allowed"] is True and first["distanceMeters"] < 3000
        assert first["nextChange"].startswith("2026-09-08T20:00")
        assert "$ 4.200" in first["whyLegal"]

        r2 = await c.get("/v1/cities/bogota/curbs/nearby",
                         params={"lat": 4.6845, "lon": -74.0528, "radius": 500, "userClass": "delivery",
                                 "at": "2026-09-08T23:30:00-05:00"})
        names = [c["name"] for c in r2.json()["curbs"]]
        assert names == [ZONE_100["name"]]


async def test_nearby_rejects_an_unknown_user_class(bogota: City):
    app, _, store = _app(_city(bogota))
    await _seed(store)
    async with _client(app) as c:
        r = await c.get("/v1/cities/bogota/curbs/nearby", params={"lat": 4.67, "lon": -74.05,
                                                                  "userClass": "helicopter"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "BAD_REQUEST"


async def test_curbs_bbox_filter_and_disabled_city(bogota: City):
    app, rt, store = _app(_city(bogota))
    await _seed(store)
    async with _client(app) as c:
        r = await c.get("/v1/cities/bogota/curbs", params={"bbox": "-74.049,4.676,-74.048,4.677"})
        assert r.status_code == 200 and [z["name"] for z in r.json()["curbs"]] == [ZONE_93["name"]]

    rt.city = bogota                                        # open mobility off
    async with _client(app) as c:
        r = await c.get("/v1/cities/bogota/curbs")
        assert r.status_code == 404 and r.json()["error"]["code"] == "OPEN_MOBILITY_DISABLED"


# ------------------------------------------------------------------ verbatim spec endpoints
async def test_cds_curbs_envelope_is_spec_shaped(bogota: City):
    app, _, store = _app(_city(bogota))
    await _seed(store)
    async with _client(app) as c:
        r = await c.get("/v1/cities/bogota/cds/curbs/zones")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(MEDIA_CDS.split(";")[0])
        assert "version=1.1" in r.headers["content-type"]
        assert r.headers["etag"] and r.headers["last-modified"]
        body = r.json()
        assert body["version"] == CDS_VERSION and body["time_zone"] == "America/Bogota"
        assert isinstance(body["last_updated"], int) and body["currency"] == "COP"
        assert body["links"]["next"] is None
        zone = body["data"]["zones"][0]
        for field in ("curb_zone_id", "geometry", "curb_policy_ids", "published_date", "last_updated_date",
                      "start_date"):
            assert field in zone, field

        rp = await c.get("/v1/cities/bogota/cds/curbs/policies")
        pol = rp.json()["data"]["policies"][0]
        assert {"curb_policy_id", "priority", "rules", "published_date"} <= set(pol)
        assert pol["rules"][0]["activity"] in (
            "parking", "loading", "unloading", "stopping", "travel",
            "no parking", "no loading", "no unloading", "no stopping", "no travel")
        assert (await c.get("/v1/cities/bogota/cds/curbs/areas")).json()["data"]["areas"] == []


async def test_spec_endpoints_answer_406_on_a_version_we_do_not_speak(bogota: City):
    app, _, store = _app(_city(bogota))
    await _seed(store)
    async with _client(app) as c:
        r = await c.get("/v1/cities/bogota/cds/curbs/zones",
                        headers={"Accept": "application/vnd.cds+json;version=9.9"})
        assert r.status_code == 406 and r.json()["error"]["code"] == "NOT_ACCEPTABLE"
        ok = await c.get("/v1/cities/bogota/cds/curbs/zones",
                         headers={"Accept": "application/vnd.cds+json;version=1.1"})
        assert ok.status_code == 200


async def test_publishing_is_gated(bogota: City):
    city = effective_city(bogota, {"openMobility": {"cds": {"enabled": True, "publish": False},
                                                    "mds": {"enabled": True, "publishPolicy": False}}})
    app, _, store = _app(city)
    await _seed(store)
    async with _client(app) as c:
        assert (await c.get("/v1/cities/bogota/cds/curbs/zones")).status_code == 404
        assert (await c.get("/v1/cities/bogota/mds/policies")).status_code == 404
        assert (await c.get("/v1/cities/bogota/curbs")).status_code == 200    # normalised view still open


async def test_mds_documents_round_trip_and_zones(bogota: City, fixtures):
    doc = json.loads((fixtures / "mds_policy_geography.json").read_text())
    app, _, store = _app(_city(bogota))
    async with _client(app) as c:
        r = await c.put("/v1/admin/cities/bogota/mds/documents", json=doc, headers=TOKEN,
                        params={"replace": "true"})
        assert r.status_code == 200 and r.json()["policies"] == 2 and r.json()["geographies"] == 2

        mp = await c.get("/v1/cities/bogota/mds/policies")
        assert mp.status_code == 200 and "version=2.1" in mp.headers["content-type"]
        body = mp.json()
        assert body["version"] == MDS_VERSION and len(body["policies"]) == 2
        pol = body["policies"][0]
        assert {"policy_id", "name", "mode_id", "rules", "start_date", "published_date"} <= set(pol)
        assert {"rule_id", "rule_type", "geographies", "states"} <= set(pol["rules"][0])

        mg = await c.get("/v1/cities/bogota/mds/geographies")
        geo = mg.json()["geographies"][0]
        assert {"geography_id", "name", "geography_json", "published_date"} <= set(geo)
        assert geo["geography_json"]["type"] == "FeatureCollection"

        z = await c.get("/v1/cities/bogota/zones", params={"at": "2026-09-11T23:00:00-05:00"})  # Friday night
        zones = z.json()["zones"]
        by_type = {x["type"]: x for x in zones}
        assert by_type["no_parking"]["geometry"]["type"] == "Polygon"
        assert by_type["no_parking"]["active"] is True                       # no time spans -> always
        speed = by_type["speed_limit"]
        assert speed["rule"]["maximum"] == 10 and speed["active"] is True    # inside the Fri 22:00-06:00 window
        assert speed["vehicleTypes"] == [] or isinstance(speed["vehicleTypes"], list)

        day = await c.get("/v1/cities/bogota/zones", params={"at": "2026-09-11T12:00:00-05:00",
                                                             "activeOnly": "true"})
        assert [x["type"] for x in day.json()["zones"]] == ["no_parking"]     # the speed rule is off at noon


def test_zone_normalisation_maps_rule_types(bogota: City, fixtures):
    doc = json.loads((fixtures / "mds_policy_geography.json").read_text())
    policies, geographies = parse_mds_documents(doc)
    out = zones_public(policies, geographies, _bog(2026, 9, 11, 23, 0), _city(bogota))
    kinds = sorted(z["type"] for z in out)
    assert kinds == ["no_parking", "speed_limit"]
    np = next(z for z in out if z["type"] == "no_parking")
    assert np["appliesTo"] == ["micromobility"] and np["vehicleTypes"] == ["bicycle", "scooter"]
    assert np["rule"]["messages"]["es-CO"].startswith("No dejes")


# ------------------------------------------------------------------ import shapes
def test_import_accepts_a_geojson_feature_collection():
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Polygon", "coordinates": [[[-74.05, 4.67], [-74.049, 4.67],
                                                          [-74.049, 4.671], [-74.05, 4.67]]]},
         "properties": {"name": "Zona azul 1", "street_name": "Cra 15",
                        "policies": [{"priority": 1, "rules": [
                            {"activity": "parking", "max_stay": 120, "max_stay_unit": "minute",
                             "rate": [{"rate": 3800, "rate_unit": "hour"}]}]}]}}]}
    zones, policies = parse_curbs_document(fc)
    assert len(zones) == 1 and len(policies) == 1
    assert zones[0]["curb_policy_ids"] == [policies[0]["curb_policy_id"]]
    assert zones[0]["name"] == "Zona azul 1"
    # a non-UUID id is derived deterministically, so re-importing does not duplicate the zone
    again, _ = parse_curbs_document(fc)
    assert again[0]["curb_zone_id"] == zones[0]["curb_zone_id"]


def test_import_rejects_an_unknown_activity():
    from app.errors import ApiError
    with pytest.raises(ApiError) as e:
        parse_curbs_document({"zones": [], "policies": [{"rules": [{"activity": "teleport"}]}]})
    assert "activity" in str(e.value)


async def test_admin_crud_and_unknown_policy_reference(bogota: City):
    app, _, store = _app(_city(bogota))
    async with _client(app) as c:
        bad = await c.put("/v1/admin/cities/bogota/curbs", headers=TOKEN,
                          json={"zones": [ZONE_93], "policies": []})
        assert bad.status_code == 422 and "unknown curb_policy_id" in bad.json()["error"]["message"]

        ok = await c.put("/v1/admin/cities/bogota/curbs", headers=TOKEN, params={"replace": "true"},
                         json={"zones": [ZONE_93, ZONE_100],
                               "policies": [PAID_PARKING, NO_STOPPING_PEAK, NIGHT_LOADING]})
        assert ok.status_code == 200 and ok.json()["zones"] == 2

        assert (await c.get("/v1/admin/cities/bogota/curbs", headers=TOKEN)).json()["count"] == 2
        assert (await c.get("/v1/admin/cities/bogota/curbs")).status_code == 401

        d = await c.delete("/v1/admin/cities/bogota/curbs", headers=TOKEN,
                           params={"zoneId": ZONE_93["curb_zone_id"]})
        assert d.status_code == 200 and d.json()["deleted"] == 1
        missing = await c.delete("/v1/admin/cities/bogota/curbs", headers=TOKEN, params={"zoneId": "nope"})
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "CURB_ZONE_NOT_FOUND"

        cleared = await c.delete("/v1/admin/cities/bogota/curbs", headers=TOKEN)
        assert cleared.json()["deleted"] == 1
        assert (await c.get("/v1/admin/cities/bogota/curbs", headers=TOKEN)).json()["count"] == 0


# ------------------------------------------------------------------ config, credentials, health
async def test_mds_provider_credentials_are_masked_and_kept(bogota: City):
    app, rt, _ = _app(bogota)
    async with _client(app) as c:
        provider = {"id": "acme", "name": "Acme Scooters", "mode": "micromobility",
                    "baseUrl": "https://acme.example.com/mds", "ingest": ["vehicles"],
                    "auth": {"kind": "oauth2", "tokenUrl": "https://acme.example.com/token"},
                    "credentials": {"clientId": "abcd1234efgh", "clientSecret": "s3cr3tvalue"}}
        r = await c.put("/v1/admin/cities/bogota/config", headers=TOKEN,
                        json={"openMobility": {"mds": {"enabled": True, "providers": [provider]}}})
        assert r.status_code == 200
        stored = rt.city.open_mobility.mds.providers[0]
        assert stored.credentials == {"clientId": "abcd1234efgh", "clientSecret": "s3cr3tvalue"}
        masked = r.json()["override"]["openMobility"]["mds"]["providers"][0]["credentials"]
        assert masked["clientId"].startswith("••••") and "abcd1234" not in json.dumps(masked)

        # echoing the masked value back keeps the stored secret
        echo = dict(provider, credentials=masked)
        r2 = await c.put("/v1/admin/cities/bogota/config", headers=TOKEN,
                         json={"openMobility": {"mds": {"enabled": True, "providers": [echo]}}})
        assert r2.status_code == 200
        assert rt.city.open_mobility.mds.providers[0].credentials["clientSecret"] == "s3cr3tvalue"

        # omitting the key entirely also keeps it
        bare = {k: v for k, v in provider.items() if k != "credentials"}
        await c.put("/v1/admin/cities/bogota/config", headers=TOKEN,
                    json={"openMobility": {"mds": {"enabled": True, "providers": [bare]}}})
        assert rt.city.open_mobility.mds.providers[0].credentials["clientSecret"] == "s3cr3tvalue"

        # an explicit null clears them
        cleared = dict(provider, credentials=None)
        await c.put("/v1/admin/cities/bogota/config", headers=TOKEN,
                    json={"openMobility": {"mds": {"enabled": True, "providers": [cleared]}}})
        assert rt.city.open_mobility.mds.providers[0].credentials == {}


async def test_config_validation_bounds(bogota: City):
    app, _, _ = _app(bogota)
    async with _client(app) as c:
        cases = [
            ({"cds": {"enabled": True, "curbs": {"source": "url"}}}, "curbs.url"),
            ({"cds": {"rateMinorUnits": 0}}, "rateMinorUnits"),
            ({"mds": {"providers": [{"id": "a", "name": "A", "baseUrl": "http://x.example"}]}}, "https"),
            ({"mds": {"providers": [{"id": "a", "name": "A", "baseUrl": "https://x.example",
                                     "auth": {"kind": "oauth2"}}]}}, "tokenUrl"),
            ({"mds": {"providers": [{"id": "a", "name": "A", "baseUrl": "https://x.example",
                                     "auth": {"kind": "jwt"}}]}}, "jwksUrl"),
            ({"mds": {"providers": [{"id": "a", "name": "A", "baseUrl": "https://x.example"},
                                    {"id": "a", "name": "B", "baseUrl": "https://y.example"}]}}, "duplicate"),
        ]
        for patch, needle in cases:
            r = await c.put("/v1/admin/cities/bogota/config", headers=TOKEN, json={"openMobility": patch})
            assert r.status_code == 422, patch
            assert needle in r.json()["error"]["message"], (patch, r.json())


async def test_public_city_and_health_expose_the_flag(bogota: City):
    app, rt, store = _app(_city(bogota))
    await _seed(store)
    async with _client(app) as c:
        pub = (await c.get("/v1/cities/bogota")).json()
        assert pub["features"]["openMobility"] is True
        assert pub["openMobility"]["cds"]["rateCurrency"] == "COP"
        assert pub["openMobility"]["cds"]["rateMinorUnits"] == 1
        # no credential ever reaches the public city document
        assert "credentials" not in json.dumps(pub)

        h = await _open_mobility_health(app, rt)
        assert h["enabled"] is True and h["cds"]["curbZones"] == 2 and h["cds"]["publishing"] is True


async def _open_mobility_health(app: FastAPI, rt: CityRuntime) -> dict:
    from app.routers.health import _open_mobility_health as fn

    class _Req:
        def __init__(self, app):
            self.app = app

    return await fn(_Req(app), rt)


async def test_no_restricted_field_leaks_into_public_responses(bogota: City):
    """A zone carrying operator-ish extras still only answers with the open plane on our normalised view."""
    app, _, store = _app(_city(bogota))
    tainted = dict(ZONE_93, data_source_operator_id=["op-1"], custom_attributes={"internal_note": "secret"})
    await _seed(store, zones=[tainted], policies=[PAID_PARKING])
    async with _client(app) as c:
        body = (await c.get("/v1/cities/bogota/curbs")).text
        assert "internal_note" not in body and "data_source_operator_id" not in body
        # the verbatim endpoint is meant to be byte-faithful, so it does echo what the city published
        spec = (await c.get("/v1/cities/bogota/cds/curbs/zones")).text
        assert "internal_note" in spec


def test_distance_is_zero_inside_a_polygon():
    assert distance_to_geometry_m(ZONE_93["geometry"], -74.0485, 4.6766) == 0.0
    far = distance_to_geometry_m(ZONE_93["geometry"], -74.10, 4.60)
    assert far and far > 5000


def test_city_now_honours_the_at_override(bogota: City):
    when = city_now(bogota, "2026-09-08T12:00:00-05:00")
    assert when.hour == 12 and str(when.tzinfo) == "America/Bogota"
    assert ms(when) == int(when.timestamp() * 1000)
