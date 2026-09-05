"""GBFS 3.0 client against a saved feed snapshot: localized names, availability, e-bikes, pricing, bbox, nearest."""
import json
from pathlib import Path

import pytest

from app.cities import BikeShareNetwork
from app.gbfs import GbfsNetwork, iso_from, pick_price_plan, text

FIX = Path(__file__).parent / "fixtures" / "gbfs"


def _fetcher(fixtures: Path):
    async def fetch(url: str) -> dict:
        name = url.rsplit("/", 1)[-1].replace(".json", "")
        name = "gbfs" if name.endswith("gbfs") else name
        return json.loads((fixtures / f"{name}.json").read_text(encoding="utf-8"))
    return fetch


def _net(nid: str = "acme", network: str = "acme_city") -> BikeShareNetwork:
    return BikeShareNetwork(id=nid, name="Acme Bikes", network=network, color="#112233",
                            gbfs_url="https://example.org/gbfs/gbfs.json")


@pytest.mark.asyncio
async def test_parses_gbfs_3_feed():
    g = GbfsNetwork("city", _net(), fetcher=_fetcher(FIX))
    await g.refresh()
    assert g.version == "3.0" and g.ttl == 30 and g.last_error is None
    assert "station_status" in g.feeds and len(g.station_info) == 6
    s = g.station("1")
    assert s["id"] == "acme:1" and s["networkId"] == "acme" and s["kind"] == "rental_station"
    assert s["name"] == "001 - CL 82 con KR 11"              # localized array -> es text
    assert s["vehiclesAvailable"] == 9 and s["docksAvailable"] == 4 and s["capacity"] == 19
    assert s["ebikesAvailable"] == 0 and s["isRenting"] is True
    assert s["lastReported"] == "2026-09-04T21:43:40Z"
    assert g.station("acme:1")["id"] == "acme:1"            # scoped id accepted too
    assert g.station("nope") is None


@pytest.mark.asyncio
async def test_detail_types_pricing_and_summary():
    g = GbfsNetwork("city", _net(), fetcher=_fetcher(FIX))
    await g.refresh()
    d = g.station("1", detail=True)
    assert [t["id"] for t in d["vehicleTypesAvailable"]][:2] == ["FIT", "CARGO"]
    assert d["vehicleTypesAvailable"][0] == {"id": "FIT", "formFactor": "bicycle", "propulsion": "human",
                                              "name": "FIT", "count": 9}
    assert d["network"]["stations"] == 6 and d["network"]["up"] is True
    assert g.vehicle_type("EFIT")["propulsion"] == "electric_assist" and g.is_electric("EFIT")
    # pricing: the cheapest real day/single plan, never a test or free promo
    pe = g.price_estimate()
    assert pe == {"amount": 11000.0, "currency": "COP", "label": "Diario", "estimated": True}
    assert g.pricing_summary().startswith("Diario $11.000")
    g.cfg = g.cfg.model_copy(update={"single_trip_price": {"amount": 4850, "currency": "COP", "label": "1 viaje"}})
    assert g.price_estimate() == {"amount": 4850.0, "currency": "COP", "label": "1 viaje", "estimated": True}
    assert g.summary()["systemId"] == "bogota_bike" and g.summary()["gbfsVersion"] == "3.0"


def test_pick_price_plan_rules():
    # real-world feed: campaign tags, tests, free promos, subsidised and partner plans, stale prices
    plans = [{"name": "Teste 06.09", "price": 9000}, {"name": "[Dez.22] 7 días gratis", "price": 0},
             {"name": "[Set.22] 1 viaje", "price": 1300}, {"name": "[Set.2] 1 viaje Sisben", "price": 712},
             {"name": "[Jan.24] 1 Viaje", "price": 4850}, {"name": "[Jan.24] 1 Viaje", "price": 4850},
             {"name": "MAS - per ride (BOG)", "price": 4850}, {"name": "[Jan.24] 1 Viaje - bff", "price": 4850},
             {"name": "[Ago.23] Diario Bogotá", "price": 11000}, {"name": "[Abr.25] Mensual Bogotá", "price": 34650}]
    best = pick_price_plan(plans)
    assert best["price"] == 4850 and "Sisben" not in best["name"]
    # no single-ride plan: the day pass; no day pass: the cheapest remaining
    assert pick_price_plan([{"name": "Diario", "price": 11000}, {"name": "Mensual", "price": 31990}])["price"] == 11000
    assert pick_price_plan([{"name": "Mensual", "price": 31990}])["name"] == "Mensual"
    assert pick_price_plan([{"name": "Free", "price": 0}]) is None


@pytest.mark.asyncio
async def test_bbox_and_nearest():
    g = GbfsNetwork("city", _net(), fetcher=_fetcher(FIX))
    await g.refresh()
    all_ = g.stations()
    assert len(all_) == 6
    box = (-74.06, 4.66, -74.05, 4.67)
    inside = g.stations(box)
    assert inside and all(box[0] <= s["lon"] <= box[2] and box[1] <= s["lat"] <= box[3] for s in inside)
    near = g.nearest(4.6658, -74.0528, 300, 5)
    assert near and near[0]["id"] == "acme:1" and near[0]["distanceMeters"] < 20
    assert near == sorted(near, key=lambda s: s["distanceMeters"])
    assert g.stations(limit=2) and len(g.stations(limit=2)) == 2


@pytest.mark.asyncio
async def test_refresh_failure_is_reported_not_raised():
    async def broken(url: str) -> dict:
        raise RuntimeError("boom")
    g = GbfsNetwork("city", _net(), fetcher=broken)
    await g.refresh()
    assert g.last_error and "boom" in g.last_error and g.up() is False
    assert g.health() == {"id": "acme", "up": False, "stations": 0, "vehiclesAvailable": 0, "ageSeconds": None,
                          "error": g.last_error}


def test_text_and_iso_helpers():
    assert text([{"text": "Hello", "language": "en"}, {"text": "Hola", "language": "es"}]) == "Hola"
    assert text([{"text": "Hello", "language": "en"}], lang="fr") == "Hello"
    assert text("plain ") == "plain" and text(None) is None and text([]) is None
    assert iso_from(1_700_000_000) == "2023-11-14T22:13:20+00:00".replace("+00:00", "Z")
    assert iso_from("2026-09-04T21:43:40.773Z") == "2026-09-04T21:43:40Z"


@pytest.mark.asyncio
async def test_two_networks_keep_distinct_ids_and_colors():
    """Several providers per city: ids, colours and station scoping never mix."""
    a = GbfsNetwork("city", _net("acme", "acme_city"), fetcher=_fetcher(FIX))
    b = GbfsNetwork("city", BikeShareNetwork(id="zed", name="Zed Scooters", network="zed_city", color="#AA00FF",
                                             gbfs_url="https://example.org/zed/gbfs.json", form_factors=["scooter"]),
                    fetcher=_fetcher(FIX))
    await a.refresh()
    await b.refresh()
    assert a.station("1")["id"] == "acme:1" and b.station("1")["id"] == "zed:1"
    assert a.summary()["color"] == "#112233" and b.summary()["color"] == "#AA00FF"
    # `scooter` is advertised only when the feed reports scooters available; the fixture stocks bicycles only
    assert b.summary()["formFactors"] == [] and b.form_factors() == []
    assert a.summary()["formFactors"] == ["bicycle"]
    assert a.mode_available("BICYCLE_RENTAL") is True and b.mode_available("SCOOTER_RENTAL") is False
    assert a.station("zed:1") is None           # a foreign scoped id is not silently accepted
