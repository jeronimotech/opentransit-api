from pathlib import Path

import pytest

from app.cities import City, expand_env, load_city_file, load_registry


def test_registry_loads_bogota_and_skips_template():
    reg = load_registry(Path("cities"))
    assert "bogota" in reg
    assert "_template" not in reg
    b = reg["bogota"]
    assert b.timezone == "America/Bogota"
    assert b.otp.feed_id == "bogota"
    assert b.component_of_agency("1") == "trunk"
    assert b.component_of_agency("7") == "cable"
    assert b.component_of_agency("zzz") == "other"


def test_scoped_ids_roundtrip(bogota: City):
    assert bogota.scoped("1234") == "bogota:1234"
    assert bogota.scoped("bogota:1234") == "bogota:1234"
    assert bogota.unscoped("bogota:1234") == "1234"
    assert bogota.scoped(None) is None


def test_public_shape_hides_feeds(bogota: City):
    pub = bogota.public()
    assert "feeds" not in pub and "otp" not in pub
    assert pub["branding"]["primaryColor"] == "#D32F2F"
    assert pub["features"]["realtimeVehicles"] is True
    assert pub["agencies"][0]["component"] == "trunk"


def test_env_interpolation(monkeypatch):
    monkeypatch.setenv("X_URL", "http://otp:9999")
    assert expand_env("a ${X_URL} b ${MISSING:-dflt} c ${MISSING2}") == "a http://otp:9999 b dflt c "


def test_bad_bbox_rejected(tmp_path: Path):
    p = tmp_path / "x.yaml"
    p.write_text("""
id: x
name: X
country: XX
timezone: UTC
center: {lat: 0, lon: 0}
bbox: [1, 1, 0, 0]
feeds: {gtfs_static_url: http://e/x.zip}
otp: {base_url: http://o, feed_id: x}
""")
    with pytest.raises(ValueError):
        load_city_file(p)


def test_id_must_match_filename(tmp_path: Path):
    p = tmp_path / "other.yaml"
    p.write_text("""
id: x
name: X
country: XX
timezone: UTC
center: {lat: 0, lon: 0}
bbox: [0, 0, 1, 1]
feeds: {gtfs_static_url: http://e/x.zip}
otp: {base_url: http://o, feed_id: x}
""")
    with pytest.raises(ValueError):
        load_city_file(p)


def test_toronto_bike_share_matches_the_otp_updater():
    """Bike Share Toronto is only reachable if the config's `network` equals the OTP updater's, so pin
    both ends: a rename on one side silently returns rentals OTP cannot resolve to a network."""
    import json

    city = load_registry(Path("cities"))["toronto"]
    assert city.features.bike_share is True and city.config.features["bike"] is True
    net = city.bike_network("bike_share_toronto")
    assert net is not None and net.id == "bike-share-toronto"
    assert net.form_factors == ["bicycle"]          # no scooters are docked in Toronto
    assert net.per_minute_price is not None and net.per_minute_price.currency == "CAD"

    updaters = json.loads(Path("otp/toronto/router-config.json").read_text())["updaters"]
    rental = [u for u in updaters if u["type"] == "vehicle-rental"]
    assert len(rental) == 1
    assert rental[0]["network"] == net.network
    assert rental[0]["url"] == net.gbfs_url
