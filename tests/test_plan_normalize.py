import json

from app.normalize import parse_duration, plan_from_otp


def test_parse_duration():
    assert parse_duration("PT2M") == 120
    assert parse_duration("-PT30S") == -30
    assert parse_duration("PT1H5M") == 3900
    assert parse_duration(None) is None
    assert parse_duration("garbage") is None


def test_plan_normalizes_itinerary(bogota, fixtures):
    data = json.loads((fixtures / "otp_plan.json").read_text())
    out = plan_from_otp(bogota, data, {"name": None, "lat": 4.75, "lon": -74.04},
                        {"name": None, "lat": 4.68, "lon": -74.05}, "2.9.0", "es")
    assert out["router"] == {"engine": "otp", "version": "2.9.0", "realtime": True}
    assert out["warnings"] == []
    assert len(out["itineraries"]) == 1
    it = out["itineraries"][0]
    assert it["id"] == "it-0"
    assert it["durationSeconds"] == 2730 and it["transfers"] == 1
    assert it["walkDistanceMeters"] == 812.4 and it["fare"] is None and it["accessible"] is None
    walk, bus, walk2 = it["legs"]
    # walking leg
    assert walk["mode"] == "WALK" and walk["transit"] is False and walk["realtime"] is False
    assert walk["startTime"] == "2026-09-04T08:02:00-05:00"
    assert walk["from"]["stopId"] is None and walk["to"]["stopId"] == "bogota:PN1"
    assert walk["steps"][1]["instruction"] == "Gira a la derecha en Calle 170"
    assert walk["geometry"] == {"encoded": "_p~iF~ps|U_ulLnnqC_mqNvxq`@", "precision": 5}
    # transit leg with realtime
    assert bus["mode"] == "BUS" and bus["transit"] is True and bus["realtime"] is True
    assert bus["startTime"] == "2026-09-04T08:14:00-05:00"     # estimated wins over scheduled
    assert bus["delaySeconds"] == 120 and bus["realtimeState"] == "UPDATED"
    assert bus["route"]["id"] == "bogota:B12" and bus["route"]["color"] == "#D32F2F"
    assert bus["route"]["component"] == "trunk" and bus["route"]["agencyId"] == "1"
    assert bus["agency"] == {"id": "1", "name": "TransMilenio Troncal"}
    assert bus["tripId"] == "bogota:T123" and bus["headsign"] == "Portal Sur"
    assert bus["from"]["component"] == "trunk" and bus["from"]["departure"] == "2026-09-04T08:14:00-05:00"
    assert bus["intermediateStops"][0]["stopId"] == "bogota:S1"
    assert bus["alerts"][0]["effect"] == "DETOUR" and bus["alerts"][0]["routeIds"] == ["bogota:B12"]
    assert bus["alerts"][0]["start"] == "2026-09-01T05:00:00Z" and bus["alerts"][0]["end"] is None
    assert bus["alerts"][0]["description"] is None
    assert walk2["mode"] == "WALK"


def test_english_instructions(bogota, fixtures):
    data = json.loads((fixtures / "otp_plan.json").read_text())
    out = plan_from_otp(bogota, data, {"lat": 0, "lon": 0}, {"lat": 0, "lon": 0}, None, "en")
    assert out["itineraries"][0]["legs"][0]["steps"][1]["instruction"] == "Turn right onto Calle 170"


def test_no_itineraries_yields_warning(bogota):
    out = plan_from_otp(bogota, {"planConnection": {"edges": [], "routingErrors": []}},
                        {"lat": 0, "lon": 0}, {"lat": 0, "lon": 0}, None)
    assert out["itineraries"] == []
    assert out["warnings"][0].startswith("NO_ITINERARIES")


def test_routing_errors_become_warnings(bogota):
    err = {"code": "OUTSIDE_BOUNDS", "description": "origin outside"}
    data = {"planConnection": {"edges": [], "routingErrors": [err]}}
    out = plan_from_otp(bogota, data, {"lat": 0, "lon": 0}, {"lat": 0, "lon": 0}, None)
    assert out["warnings"] == ["OUTSIDE_BOUNDS: origin outside"]


def test_response_matches_pydantic_model(bogota, fixtures):
    from app.models import PlanResponse
    data = json.loads((fixtures / "otp_plan.json").read_text())
    out = plan_from_otp(bogota, data, {"lat": 4.75, "lon": -74.04}, {"lat": 4.68, "lon": -74.05}, "2.9.0")
    model = PlanResponse.model_validate(out)
    dumped = model.model_dump(by_alias=True)
    assert "from" in dumped and "from" in dumped["itineraries"][0]["legs"][0]
    assert dumped["itineraries"][0]["legs"][1]["delaySeconds"] == 120


def test_pattern_headsign_and_direction_fallbacks(bogota):
    from app.normalize import pattern_from_otp
    p = {"code": "bogota:10895::01", "headsign": None, "directionId": -1, "patternGeometry": {"points": "abc"},
         "stops": [{"gtfsId": "bogota:1", "name": "First"}, {"gtfsId": "bogota:2", "name": "Portal Sur 10-1"}]}
    out = pattern_from_otp(bogota, p)
    assert out["headsign"] == "Portal Sur 10-1" and out["directionId"] is None
    assert out["geometry"] == {"encoded": "abc", "precision": 5} and len(out["stops"]) == 2
    assert pattern_from_otp(bogota, {**p, "headsign": "Norte", "directionId": 1})["directionId"] == 1
    assert pattern_from_otp(bogota, {**p, "stops": []})["headsign"] is None


def test_merge_departures_dedupes_by_trip_and_sorts():
    from app.normalize import merge_departures
    deps = [
        {"tripId": "t2", "scheduledTime": "2026-09-04T10:05:00Z", "realtimeTime": None},
        {"tripId": "t1", "scheduledTime": "2026-09-04T10:00:00Z", "realtimeTime": "2026-09-04T10:07:00Z"},
        {"tripId": "t2", "scheduledTime": "2026-09-04T10:06:00Z", "realtimeTime": None},   # same trip, other platform
        {"tripId": None, "scheduledTime": "2026-09-04T10:01:00Z", "realtimeTime": None},
    ]
    out = merge_departures(deps)
    assert [d["tripId"] for d in out] == [None, "t2", "t1"]


def test_apply_endpoint_names(bogota, fixtures):
    from app.normalize import apply_endpoint_names
    data = json.loads((fixtures / "otp_plan.json").read_text())
    out = plan_from_otp(bogota, data, {"name": None, "lat": 1, "lon": 2}, {"name": None, "lat": 3, "lon": 4}, None)
    out = apply_endpoint_names(out, "Casa", None)
    legs = out["itineraries"][0]["legs"]
    assert out["from"]["name"] == "Casa" and legs[0]["from"]["name"] == "Casa"
    assert out["to"]["name"] is None and legs[-1]["to"]["name"] == "Destination"
    assert legs[0]["to"]["name"] == "Portal Norte"          # stops are never renamed
    out = apply_endpoint_names(out, None, "Oficina")
    assert legs[-1]["to"]["name"] == "Oficina"
