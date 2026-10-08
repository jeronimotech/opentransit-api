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


def test_offline_is_always_in_the_public_shape(bogota: City):
    """Present or absent, the key is there, so the client branches on a value and not on a KeyError.

    This asserted `is None` until the day the bundles were published, which made it a test of the
    world rather than of the code. What matters is the contract: the key exists, and when a bundle
    is configured it round-trips with the size the app shows before asking anyone to spend it."""
    pub = bogota.public()
    assert "offline" in pub
    if bogota.offline is None:
        assert pub["offline"] is None
    else:
        assert pub["offline"]["url"] == bogota.offline.url
        assert pub["offline"]["bytes"] == bogota.offline.bytes > 0
        assert pub["offline"]["formatVersion"] == 1


def test_a_city_with_no_bundle_offers_no_download(tmp_path: Path):
    """The app must show "not available yet" rather than a button that 404s."""
    p = tmp_path / "testville.yaml"
    p.write_text(
        """
id: testville
name: Testville
country: XX
timezone: UTC
center: {lat: 0.1, lon: 0.1}
bbox: [-1, -1, 1, 1]
feeds: {gtfs_static_url: https://example.com/gtfs.zip}
otp: {base_url: http://localhost:8080, feed_id: testville}
""")
    c = load_city_file(p)
    assert c.offline is None
    assert c.public()["offline"] is None


def test_offline_bundle_publishes_its_size_before_the_download_starts(tmp_path: Path):
    """The app has to say "5.4 MB" before a rider commits, and has to ask first on a metered
    connection — a question that needs the number to be worth asking. So the size is configured
    rather than discovered with a HEAD request the download would then race."""
    p = tmp_path / "testville.yaml"
    p.write_text(
        """
id: testville
name: Testville
country: XX
timezone: UTC
center: {lat: 0.1, lon: 0.1}
bbox: [-1, -1, 1, 1]
feeds: {gtfs_static_url: https://example.com/gtfs.zip}
otp: {base_url: http://localhost:8080, feed_id: testville}
offline:
  url: https://example.com/releases/offline-bundle.json.gz
  bytes: 5672345
  departures: 9560672
  built_at: "2026-10-07"
""")
    c = load_city_file(p)
    pub = c.public()["offline"]
    assert pub == {
        "url": "https://example.com/releases/offline-bundle.json.gz",
        "bytes": 5672345,
        "formatVersion": 1,
        "departures": 9560672,
        "builtAt": "2026-10-07",
    }


def test_a_zero_byte_bundle_is_a_misconfiguration_not_an_empty_download(tmp_path: Path):
    p = tmp_path / "testville.yaml"
    p.write_text(
        """
id: testville
name: Testville
country: XX
timezone: UTC
center: {lat: 0.1, lon: 0.1}
bbox: [-1, -1, 1, 1]
feeds: {gtfs_static_url: https://example.com/gtfs.zip}
otp: {base_url: http://localhost:8080, feed_id: testville}
offline: {url: https://example.com/b.json.gz, bytes: 0}
""")
    with pytest.raises(ValueError):
        load_city_file(p)
