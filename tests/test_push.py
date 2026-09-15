"""v2.3 — pushes for scheduled trips: anonymous device registration, the silent wake-up at the instants a
phone asked for, and alert pushes for the routes it follows."""
import datetime as dt
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admin_config import MemoryConfigStore, effective_city
from app.cities import City
from app.errors import install_error_handlers
from app.push import (
    ApnsClient,
    MemoryPushDeviceStore,
    alert_payload,
    alerts_to_push,
    normalize_registration,
    push_alerts,
    push_wakes,
)
from app.routers import push
from app.rt import RTCache
from app.runtime import CityRuntime

NOW = dt.datetime(2026, 9, 15, 11, 50, tzinfo=dt.UTC)
KEY = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
TOKEN = "ab" * 32


def _client(handler) -> ApnsClient:
    return ApnsClient(key_id="KEYID1", team_id="TEAM1", private_key=KEY, bundle_id="com.jeronimotech.opentransit",
                      transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ registration


def test_registration_keeps_only_what_a_phone_may_say():
    body = {"token": TOKEN.upper(), "platform": "ios", "env": "sandbox", "locale": "es-CO",
            "wakeAt": ["2026-09-16T11:32:00Z", "2026-09-01T00:00:00Z", "2026-12-01T00:00:00Z", "junk"],
            "routes": ["bogota:G30", "bogota:J23", ""], "userId": "nope", "lat": 4.6}
    d = normalize_registration(body, city="bogota", now=NOW)
    assert d["token"] == TOKEN and d["env"] == "sandbox" and d["locale"] == "es-CO"
    assert d["wake_at"] == ["2026-09-16T11:32:00+00:00"]          # past and far-future instants dropped
    assert d["routes"] == ["bogota:G30", "bogota:J23"]
    assert set(d) == {"token", "platform", "env", "city", "locale", "wake_at", "routes"}
    with pytest.raises(ValueError):
        normalize_registration({"token": "not-hex"}, city="bogota", now=NOW)
    with pytest.raises(ValueError):
        normalize_registration({"token": TOKEN, "platform": "android"}, city="bogota", now=NOW)


# ------------------------------------------------------------------ APNs


@pytest.mark.anyio
async def test_apns_request_shape_and_token_auth():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200)

    c = _client(handler)
    ok, reason = await c.send(TOKEN, {"aps": {"content-available": 1}}, background=True, collapse_id="tripRefresh")
    assert ok and reason is None and c.sent == 1
    req = seen[0]
    assert req.url == httpx.URL(f"https://api.push.apple.com/3/device/{TOKEN}")
    assert req.headers["apns-topic"] == "com.jeronimotech.opentransit"
    assert req.headers["apns-push-type"] == "background" and req.headers["apns-priority"] == "5"
    assert req.headers["apns-collapse-id"] == "tripRefresh"
    bearer = req.headers["authorization"].split(" ", 1)[1]
    assert jwt.get_unverified_header(bearer) == {"alg": "ES256", "kid": "KEYID1", "typ": "JWT"}
    assert jwt.decode(bearer, options={"verify_signature": False})["iss"] == "TEAM1"
    # sandbox builds go to the sandbox host; an alert push is priority 10
    await c.send(TOKEN, {"aps": {"alert": "x"}}, env="sandbox")
    assert seen[1].url.host == "api.sandbox.push.apple.com" and seen[1].headers["apns-priority"] == "10"


@pytest.mark.anyio
async def test_a_dead_token_is_reported_and_transport_errors_are_counted():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"reason": "Unregistered"})

    c = _client(handler)
    ok, reason = await c.send(TOKEN, {})
    assert not ok and reason == "Unregistered" and c.failed == 1 and c.last_error == "410 Unregistered"

    def boom(req):
        raise httpx.ConnectError("down")

    ok, reason = await _client(boom).send(TOKEN, {})
    assert not ok and reason == "transport"


# ------------------------------------------------------------------ the passes


@pytest.mark.anyio
async def test_wakes_are_pushed_once_when_due_and_dead_tokens_are_forgotten():
    store = MemoryPushDeviceStore()
    await store.upsert(normalize_registration({"token": TOKEN, "wakeAt": ["2026-09-15T11:49:30Z", "2026-09-16T11:32:00Z"]},
                                              city="bogota", now=NOW))
    await store.upsert(normalize_registration({"token": "cd" * 32, "wakeAt": ["2026-09-15T11:49:00Z"]},
                                              city="bogota", now=NOW))
    sent = []

    def handler(req):
        sent.append(req.url.path)
        return httpx.Response(410, json={"reason": "BadDeviceToken"}) if "cd" * 32 in req.url.path else httpx.Response(200)

    c = _client(handler)
    assert await push_wakes(store, c, "bogota", NOW) == 1
    assert len(sent) == 2
    assert "cd" * 32 not in store.devices                                           # dead token dropped
    assert store.devices[TOKEN]["wake_at"] == ["2026-09-16T11:32:00+00:00"]        # the due instant consumed
    assert await push_wakes(store, c, "bogota", NOW) == 0                           # not pushed twice


@pytest.mark.anyio
async def test_new_alerts_reach_the_devices_following_their_routes_once():
    store = MemoryPushDeviceStore()
    await store.upsert(normalize_registration({"token": TOKEN, "routes": ["bogota:G30"]}, city="bogota", now=NOW))
    await store.upsert(normalize_registration({"token": "ef" * 32, "routes": ["bogota:J23"]}, city="bogota", now=NOW))
    payloads = []

    def handler(req):
        payloads.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200)

    c = _client(handler)
    alerts = [{"id": "a1", "header": "Desvío en la Calle 26", "description": "Hasta las 18:00", "routeIds": ["bogota:G30"]},
              {"id": "a2", "header": "Sin rutas", "routeIds": []}]
    names = {"bogota:G30": "G30"}
    seen: set[str] = set()
    counts: dict[str, int] = {}
    assert await push_alerts(store, c, "bogota", alerts, names, seen, counts) == 1
    path, body = payloads[0]
    assert TOKEN in path
    assert body["aps"]["alert"] == {"title": "G30: Desvío en la Calle 26", "body": "Hasta las 18:00"}
    assert body["kind"] == "routeAlert" and body["location"] == "/bogota/alerts" and body["routeIds"] == ["bogota:G30"]
    assert seen == {"a1"}
    # the same alert again: nothing; a device remembers what it received even across restarts
    assert await push_alerts(store, c, "bogota", alerts, names, set(), counts) == 0
    assert alerts_to_push(alerts, set()) == [alerts[0]]


def test_alert_payload_without_route_names():
    p = alert_payload({"id": "x", "header": "Cierre", "routeIds": ["r"]}, [], "en")
    assert p["aps"]["alert"]["title"] == "Cierre" and p["aps"]["alert"]["body"] == "Cierre"


# ------------------------------------------------------------------ endpoints


def _app(city: City):
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(push.router)
    rt = CityRuntime(city=city, rt=RTCache(city), otp=None)  # type: ignore[arg-type]
    app.state.cities = {"bogota": rt}
    app.state.config_store = MemoryConfigStore()
    app.state.push_devices = MemoryPushDeviceStore()
    return app


@pytest.mark.anyio
async def test_registration_is_accepted_and_ignored_until_the_city_has_apns(bogota: City):
    assert bogota.config.push.reminders_active is False          # no key in the test environment
    app = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/v1/cities/bogota/push/devices", json={"token": TOKEN, "wakeAt": ["2026-09-16T11:32:00Z"]})
        assert r.status_code == 202 and r.json()["serverPush"] is False
        assert app.state.push_devices.devices == {}
        r = await c.put("/v1/cities/bogota/push/devices", json={"token": "zz"})
        assert r.status_code == 422


@pytest.mark.anyio
async def test_registration_is_stored_when_reminders_are_active(bogota: City):
    city = bogota.model_copy(update={"config": bogota.config.model_copy(update={
        "push": bogota.config.push.model_copy(update={"reminders": True, "apns": bogota.config.push.apns.model_copy(
            update={"key_id": "K", "team_id": "T", "bundle_id": "b", "key_p8": KEY})})})})
    assert city.config.push.reminders_active
    assert city.public()["config"]["push"]["reminders"] is True
    app = _app(city)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/v1/cities/bogota/push/devices", json={"token": TOKEN, "env": "sandbox",
                                                                "wakeAt": ["2026-09-16T11:32:00Z"], "routes": ["bogota:G30"]})
        assert r.status_code == 202 and r.json() == {"accepted": True, "serverPush": True, "wakes": 1, "routes": 1}
        assert app.state.push_devices.devices[TOKEN]["env"] == "sandbox"
        r = await c.delete(f"/v1/cities/bogota/push/devices/{TOKEN}")
        assert r.status_code == 204 and app.state.push_devices.devices == {}
    assert "APNS" not in json.dumps(city.public()) and KEY[:30] not in json.dumps(city.public())


def test_the_admin_switch_does_not_expose_credentials(bogota: City):
    city = effective_city(bogota, {"config": {"push": {"enabled": False, "reminders": True}}})
    assert city.config.push.reminders is True
    assert "apns" not in city.public()["config"]["push"]
