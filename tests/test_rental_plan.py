"""Shared-vehicle legs end to end: mode parsing, OTP variables, normalizer, live enrichment, fares, admin config,
OTP updater generation — with one and with two networks configured."""
import importlib.util
import json
from pathlib import Path

import pytest

from app.admin_config import effective_city
from app.cities import BikeShareNetwork, City, Mobility
from app.errors import ApiError
from app.features import estimate_fare
from app.normalize import enrich_rental, plan_from_otp
from app.routers.plan import build_variables, parse_modes

FIX = Path(__file__).parent / "fixtures"


def _with_networks(city: City, n: int = 1) -> City:
    nets = [BikeShareNetwork(id="acme", name="Acme Bikes", network="acme_city", color="#112233",
                             gbfs_url="https://example.org/gbfs.json")]
    if n > 1:
        nets.append(BikeShareNetwork(id="zed", name="Zed Scooters", network="zed_city", color="#AA00FF",
                                     gbfs_url="https://example.org/zed.json", form_factors=["scooter"]))
    return city.model_copy(update={"mobility": Mobility(bike_share=nets)})


def _rental_fixture_as(network: str) -> dict:
    """The saved OTP response, re-scoped to a given updater network id."""
    raw = (FIX / "otp_plan_rental.json").read_text(encoding="utf-8")
    return json.loads(raw.replace("tembici_bogota", network))


# ------------------------------------------------------------------ modes
def test_parse_modes_accepts_rental_tokens():
    transit, street = parse_modes("TRANSIT,WALK,BIKE_RENTAL", ["BUS", "CABLE_CAR"])
    assert transit == ["BUS", "CABLE_CAR"] and street == ["WALK", "BICYCLE_RENTAL"]
    assert parse_modes("BICYCLE_RENTAL", [])[1] == ["BICYCLE_RENTAL"]
    assert parse_modes("SCOOTER_RENTAL,WALK", [])[1] == ["SCOOTER_RENTAL", "WALK"]
    with pytest.raises(ApiError):
        parse_modes("BIKE_SHARE", [])


def _vars(transit, street):
    import datetime as dt
    return build_variables(from_lat=1, from_lon=2, to_lat=3, to_lon=4, when=dt.datetime(2026, 9, 4, 8, tzinfo=dt.UTC),
                           arrive_by=False, transit=transit, street=street, wheelchair=False, num=3, locale="es",
                           walk_reluctance=None)["modes"]


def test_rental_is_access_egress_and_direct_with_transit():
    m = _vars(["BUS"], ["WALK", "BICYCLE_RENTAL"])
    assert m["transit"]["access"] == ["WALK", "BICYCLE_RENTAL"] and m["transit"]["egress"] == ["WALK", "BICYCLE_RENTAL"]
    assert m["transit"]["transfer"] == ["WALK"] and m["direct"] == ["WALK", "BICYCLE_RENTAL"]
    assert "directOnly" not in m


def test_rental_is_direct_only_without_transit():
    m = _vars([], ["WALK", "SCOOTER_RENTAL"])
    assert m == {"direct": ["WALK", "SCOOTER_RENTAL"], "directOnly": True}
    assert _vars([], ["WALK"]) == {"direct": ["WALK"], "directOnly": True}      # unchanged default


# ------------------------------------------------------------------ normalizer
def test_rental_leg_from_real_otp_response(bogota: City):
    city = _with_networks(bogota)
    data = _rental_fixture_as("acme_city")
    prices = {"acme": {"amount": 11000, "currency": "COP", "label": "Diario", "estimated": True}}
    out = plan_from_otp(city, data, {"name": "A", "lat": 4.67, "lon": -74.04},
                        {"name": "B", "lat": 4.68, "lon": -74.05}, "2.9.0", "es", prices)
    it = out["itineraries"][0]
    assert it["rentalLegs"] == 1 and it["modesUsed"] == ["WALK", "BICYCLE_RENTAL"]
    walk, bike, walk2 = it["legs"]
    assert bike["mode"] == "BICYCLE" and bike["transit"] is False
    r = bike["rental"]
    assert r["networkId"] == "acme" and r["networkName"] == "Acme Bikes" and r["color"] == "#112233"
    assert r["vehicleType"] == "bicycle" and r["freeFloating"] is False
    assert r["pickup"]["stationId"].startswith("acme:") and r["dropoff"]["stationId"].startswith("acme:")
    assert r["pickup"]["name"] and r["pickup"]["vehiclesAvailable"] is not None      # OTP's own counts
    assert r["priceEstimate"] == prices["acme"]
    assert walk["to"]["rentalStationId"] == r["pickup"]["stationId"]
    assert walk2["from"]["rentalStationId"] == r["dropoff"]["stationId"]
    assert walk["rental"] is None and walk2["rental"] is None
    # fares: one rental pass, no transit
    assert it["fare"]["amount"] == 11000 and it["fare"]["breakdown"] == [
        {"label": "Acme Bikes · Diario", "amount": 11000, "route": None, "kind": "rental"}]


def test_enrich_rental_uses_live_cache_and_strips_private_keys(bogota: City):
    city = _with_networks(bogota)
    out = plan_from_otp(city, _rental_fixture_as("acme_city"), {"name": None, "lat": 0, "lon": 0},
                        {"name": None, "lat": 0, "lon": 0}, None, "es", {})
    seen = []

    def lookup(otp_network, raw):
        seen.append((otp_network, raw))
        return {"name": "LIVE NAME", "vehiclesAvailable": 42, "docksAvailable": 1,
                "lastReported": "2026-09-04T00:00:00Z"}

    enrich_rental(out, lookup)
    r = out["itineraries"][0]["legs"][1]["rental"]
    assert seen and all(n == "acme_city" for n, _ in seen)
    assert r["pickup"] == {"stationId": r["pickup"]["stationId"], "name": "LIVE NAME", "lat": r["pickup"]["lat"],
                           "lon": r["pickup"]["lon"], "vehiclesAvailable": 42, "docksAvailable": 1,
                           "lastReported": "2026-09-04T00:00:00Z"}
    assert "_otpNetwork" not in r["dropoff"] and "_raw" not in r["dropoff"]


def test_unknown_network_degrades_gracefully(bogota: City):
    """A rental leg from a network the city does not configure still comes back, unscoped and uncoloured."""
    out = plan_from_otp(_with_networks(bogota), _rental_fixture_as("other_net"), {"name": None, "lat": 0, "lon": 0},
                        {"name": None, "lat": 0, "lon": 0}, None, "es", {})
    r = out["itineraries"][0]["legs"][1]["rental"]
    assert r["networkId"] == "other_net" and r["color"] is None and r["priceEstimate"] is None
    assert not r["pickup"]["stationId"].startswith("acme:")


# ------------------------------------------------------------------ fares with transit + rental
def test_fare_combines_transit_and_one_rental_pass_per_network(bogota: City):
    legs = [
        {"transit": False, "rental": {"networkId": "acme", "networkName": "Acme", "priceEstimate":
                                      {"amount": 11000, "currency": "COP", "label": "Diario"}}},
        {"transit": True, "startTime": "2026-09-04T08:10:00-05:00", "route": {"shortName": "G12"}},
        {"transit": False, "rental": {"networkId": "acme", "networkName": "Acme", "priceEstimate":
                                      {"amount": 11000, "currency": "COP", "label": "Diario"}}},
    ]
    f = estimate_fare(bogota, legs, "es")
    assert f["amount"] == 3200 + 11000
    assert [b["kind"] for b in f["breakdown"]] == ["transit", "rental"]        # second ride is on the same pass
    assert f["breakdown"][1]["label"] == "Acme · Diario"


def test_fare_without_city_fares_but_with_rental(bogota: City):
    city = bogota.model_copy(update={"fares": None})
    legs = [{"transit": False, "rental": {"networkId": "acme", "priceEstimate": {"amount": 5, "currency": "USD"}}}]
    assert estimate_fare(city, legs)["currency"] == "USD"
    assert estimate_fare(city, [{"transit": True}]) is None


# ------------------------------------------------------------------ admin: mobility is editable, validated
def test_admin_mobility_override_two_networks(bogota: City):
    city = effective_city(bogota, {"mobility": {"bikeShare": [
        {"id": "acme", "name": "Acme", "network": "acme_city", "gbfsUrl": "https://a.example/gbfs.json"},
        {"id": "zed", "name": "Zed", "network": "zed_city", "gbfsUrl": "https://z.example/gbfs.json",
         "color": "#AA00FF", "formFactors": ["scooter"]}]}})
    assert [n.id for n in city.mobility.bike_share] == ["acme", "zed"]
    assert city.features.bike_share is True
    assert city.bike_network("zed_city").color == "#AA00FF" and city.bike_network("acme").network == "acme_city"
    pub = city.public()["mobility"]["bikeShare"]
    assert pub[1]["formFactors"] == ["scooter"] and pub[0]["color"] == "#00A859"
    # removing every network turns the feature off again
    assert effective_city(bogota, {"mobility": {"bikeShare": []}}).features.bike_share is False


def test_admin_mobility_validation(bogota: City):
    with pytest.raises(ApiError) as e:
        effective_city(bogota, {"mobility": {"bikeShare": [{"id": "x", "name": "X", "network": "n",
                                                            "gbfsUrl": "http://insecure/gbfs.json"}]}})
    assert e.value.status == 422 and e.value.message.startswith("mobility.bikeShare.0.gbfsUrl:")
    with pytest.raises(ApiError) as e:
        effective_city(bogota, {"mobility": {"bikeShare": [{"id": "x", "name": "X", "network": "n",
                                                            "gbfsUrl": "https://ok/g.json", "color": "green"}]}})
    assert e.value.message.startswith("mobility.bikeShare.0.color:")
    with pytest.raises(ApiError) as e:
        effective_city(bogota, {"mobility": {"bikeShare": [
            {"id": "dup", "name": "A", "network": "a", "gbfsUrl": "https://a/g.json"},
            {"id": "dup", "name": "B", "network": "b", "gbfsUrl": "https://b/g.json"}]}})
    assert "duplicate network id" in e.value.message


# ------------------------------------------------------------------ OTP updaters are generated from config
def _updaters_module():
    script = Path(__file__).parent.parent / "scripts" / "otp-updaters.py"
    spec = importlib.util.spec_from_file_location("otp_updaters", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_otp_updaters_generated_per_network_and_idempotent():
    m = _updaters_module()
    city = {"locale": "es-CO", "mobility": {"bike_share": [
        {"id": "acme", "network": "acme_city", "gbfs_url": "https://a.example/gbfs.json"},
        {"id": "zed", "network": "zed_city", "gbfs_url": "https://z.example/gbfs.json"}]}}
    ups = m.updaters_for(city)
    assert [u["network"] for u in ups] == ["acme_city", "zed_city"]
    assert ups[0] == {"type": "vehicle-rental", "sourceType": "gbfs", "network": "acme_city",
                      "url": "https://a.example/gbfs.json", "language": "es", "frequency": "60s",
                      "allowKeepingRentedVehicleAtDestination": False, "geofencingZones": True}
    rc = {"updaters": [{"type": "stop-time-updater", "url": "x"},
                       {"type": "vehicle-rental", "network": "stale_city", "url": "old"}]}
    merged = m.merge(rc, ups)
    assert [u["type"] for u in merged["updaters"]] == ["stop-time-updater", "vehicle-rental", "vehicle-rental"]
    assert m.merge(merged, ups) == merged                                    # idempotent
    assert m.updaters_for({"mobility": {}}) == []
