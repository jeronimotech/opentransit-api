"""v2.3/v2.5 — pushes for scheduled trips: anonymous device registration, the silent wake-up at the instants
a phone asked for, and alert pushes for the routes it follows — over APNs for iOS and FCM for Android."""
import base64
import datetime as dt
import json
import urllib.parse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admin_config import MemoryConfigStore, effective_city
from app.cities import City, FcmConfig
from app.errors import install_error_handlers
from app.push import (
    DEAD_REASONS,
    ApnsClient,
    FcmClient,
    MemoryPushDeviceStore,
    PushMessage,
    alert_payload,
    alerts_to_push,
    normalize_registration,
    push_alerts,
    push_wakes,
    silent_wake_payload,
)
from app.routers import push
from app.rt import RTCache
from app.runtime import CityRuntime

NOW = dt.datetime(2026, 9, 15, 11, 50, tzinfo=dt.UTC)
KEY = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
TOKEN = "ab" * 32
# FCM registration tokens are long, mixed-case and carry ':' and '-' — nothing like an APNs token.
ANDROID_TOKEN = "cXy7_d-Zk1M:APA91bH-Ab3Cd4Ef5Gh6Ij7Kl8Mn9Op0Qr1St2Uv3Wx4Yz"
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def _client(handler) -> ApnsClient:
    return ApnsClient(key_id="KEYID1", team_id="TEAM1", private_key=KEY, bundle_id="com.jeronimotech.opentransit",
                      transport=httpx.MockTransport(handler))


def _fcm(handler) -> FcmClient:
    return FcmClient(project_id="opentransit-1", client_email="push@opentransit-1.iam.gserviceaccount.com",
                     private_key=RSA_KEY, transport=httpx.MockTransport(handler))


def _fcm_ok(on_send=None):
    """A mock that answers the OAuth exchange and then the send."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        if on_send is not None:
            return on_send(req)
        return httpx.Response(200, json={"name": "projects/opentransit-1/messages/1"})
    return handler


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
        normalize_registration({"token": TOKEN, "platform": "windows"}, city="bogota", now=NOW)


def test_an_android_token_keeps_its_case_and_its_punctuation():
    d = normalize_registration({"token": ANDROID_TOKEN, "platform": "android"}, city="bogota", now=NOW)
    assert d["token"] == ANDROID_TOKEN and d["platform"] == "android"
    # lowercasing an FCM token makes it undeliverable, which is what the iOS path does to hex
    assert d["token"] != d["token"].lower()
    with pytest.raises(ValueError):
        normalize_registration({"token": "has spaces", "platform": "android"}, city="bogota", now=NOW)
    with pytest.raises(ValueError):
        normalize_registration({"token": "x" * 513, "platform": "android"}, city="bogota", now=NOW)


# ------------------------------------------------------------------ APNs


@pytest.mark.anyio
async def test_apns_request_shape_and_token_auth():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200)

    c = _client(handler)
    ok, reason = await c.send(TOKEN, silent_wake_payload())
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
    await c.send(TOKEN, PushMessage(kind="routeAlert", title="x", body="y"), env="sandbox")
    assert seen[1].url.host == "api.sandbox.push.apple.com" and seen[1].headers["apns-priority"] == "10"


@pytest.mark.anyio
async def test_a_dead_token_is_reported_and_transport_errors_are_counted():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"reason": "Unregistered"})

    c = _client(handler)
    ok, reason = await c.send(TOKEN, silent_wake_payload())
    assert not ok and reason == "Unregistered" and c.failed == 1 and c.last_error == "410 Unregistered"

    def boom(req):
        raise httpx.ConnectError("down")

    ok, reason = await _client(boom).send(TOKEN, silent_wake_payload())
    assert not ok and reason == "transport"


# ------------------------------------------------------------------ the passes


@pytest.mark.anyio
async def test_wakes_are_pushed_once_when_due_and_dead_tokens_are_forgotten():
    store = MemoryPushDeviceStore()
    await store.upsert(normalize_registration({"token": TOKEN,
                                               "wakeAt": ["2026-09-15T11:49:30Z", "2026-09-16T11:32:00Z"]},
                                              city="bogota", now=NOW))
    await store.upsert(normalize_registration({"token": "cd" * 32, "wakeAt": ["2026-09-15T11:49:00Z"]},
                                              city="bogota", now=NOW))
    sent = []

    def handler(req):
        sent.append(req.url.path)
        if "cd" * 32 in req.url.path:
            return httpx.Response(410, json={"reason": "BadDeviceToken"})
        return httpx.Response(200)

    c = _client(handler)
    assert await push_wakes(store, {"ios": c}, "bogota", NOW) == 1
    assert len(sent) == 2
    assert "cd" * 32 not in store.devices                                           # dead token dropped
    assert store.devices[TOKEN]["wake_at"] == ["2026-09-16T11:32:00+00:00"]        # the due instant consumed
    assert await push_wakes(store, {"ios": c}, "bogota", NOW) == 0                           # not pushed twice


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
    alerts = [{"id": "a1", "header": "Desvío en la Calle 26", "description": "Hasta las 18:00",
               "routeIds": ["bogota:G30"]},
              {"id": "a2", "header": "Sin rutas", "routeIds": []}]
    names = {"bogota:G30": "G30"}
    seen: set[str] = set()
    counts: dict[str, int] = {}
    assert await push_alerts(store, {"ios": c}, "bogota", alerts, names, seen, counts) == 1
    path, body = payloads[0]
    assert TOKEN in path
    assert body["aps"]["alert"] == {"title": "G30: Desvío en la Calle 26", "body": "Hasta las 18:00"}
    assert body["kind"] == "routeAlert" and body["location"] == "/bogota/alerts" and body["routeIds"] == ["bogota:G30"]
    assert seen == {"a1"}
    # the same alert again: nothing; a device remembers what it received even across restarts
    assert await push_alerts(store, {"ios": c}, "bogota", alerts, names, set(), counts) == 0
    assert alerts_to_push(alerts, set()) == [alerts[0]]


def test_alert_payload_without_route_names():
    p = alert_payload({"id": "x", "header": "Cierre", "routeIds": ["r"]}, [], "en").apns_payload()
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
        tomorrow = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat()   # the router uses the real clock
        r = await c.put("/v1/cities/bogota/push/devices", json={"token": TOKEN, "env": "sandbox",
                                                                "wakeAt": [tomorrow],
                                                                "routes": ["bogota:G30"]})
        assert r.status_code == 202 and r.json() == {"accepted": True, "serverPush": True, "wakes": 1, "routes": 1}
        assert app.state.push_devices.devices[TOKEN]["env"] == "sandbox"
        r = await c.delete(f"/v1/cities/bogota/push/devices/{TOKEN}")
        assert r.status_code == 204 and app.state.push_devices.devices == {}
    assert "APNS" not in json.dumps(city.public()) and KEY[:30] not in json.dumps(city.public())


def test_the_admin_switch_does_not_expose_credentials(bogota: City):
    city = effective_city(bogota, {"config": {"push": {"enabled": False, "reminders": True}}})
    assert city.config.push.reminders is True
    assert "apns" not in city.public()["config"]["push"]


def test_the_key_may_arrive_base64_on_one_line(bogota: City):
    import base64

    from app.cities import ApnsConfig
    assert ApnsConfig(key_p8=base64.b64encode(KEY.encode()).decode()).private_key() == KEY
    assert ApnsConfig(key_p8=KEY.replace("\n", "\\n")).private_key() == KEY
    assert ApnsConfig(key_p8="not base64 !!").private_key() is None


# ------------------------------------------------------------------ FCM (Android)


@pytest.mark.anyio
async def test_fcm_exchanges_a_service_account_jwt_and_sends_data_only():
    seen = []

    def on_send(req):
        seen.append(req)
        return httpx.Response(200, json={"name": "projects/opentransit-1/messages/1"})

    exchanges = []

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            exchanges.append(req)
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        return on_send(req)

    c = _fcm(handler)
    ok, reason = await c.send(ANDROID_TOKEN, silent_wake_payload())
    assert ok and reason is None and c.sent == 1

    assertion = dict(x.split("=", 1) for x in exchanges[0].content.decode().split("&"))["assertion"]
    claims = jwt.decode(urllib.parse.unquote(assertion), options={"verify_signature": False})
    assert claims["iss"] == "push@opentransit-1.iam.gserviceaccount.com"
    assert claims["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
    assert jwt.get_unverified_header(urllib.parse.unquote(assertion))["alg"] == "RS256"

    req = seen[0]
    assert req.url == httpx.URL("https://fcm.googleapis.com/v1/projects/opentransit-1/messages:send")
    assert req.headers["authorization"] == "Bearer ya29.test"
    msg = json.loads(req.content)["message"]
    assert msg["token"] == ANDROID_TOKEN
    assert msg["android"] == {"priority": "high", "ttl": "600s", "collapse_key": "tripRefresh"}
    # data-only: no `notification` block, so the app renders the notification in its own language
    assert "notification" not in msg
    assert msg["data"] == {"kind": "tripRefresh"}

    # the access token is reused rather than re-minted on every send
    await c.send(ANDROID_TOKEN, silent_wake_payload())
    assert len(exchanges) == 1


@pytest.mark.anyio
async def test_fcm_values_are_all_strings_and_dead_tokens_are_named():
    msg = alert_payload({"id": "a1", "header": "Deviazione", "description": "Fino alle 18:00",
                         "routeIds": ["roma:64", "roma:40"]}, ["64", "40"], "it")
    data = msg.fcm_data()
    assert all(isinstance(v, str) for v in data.values())
    assert json.loads(data["routeIds"]) == ["roma:64", "roma:40"]        # a list survives as JSON
    assert data["title"] == "64, 40: Deviazione" and data["locale"] == "it"

    def gone(req):
        return httpx.Response(404, json={"error": {"status": "NOT_FOUND", "details": [
            {"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": "UNREGISTERED"}]}})

    c2 = _fcm(_fcm_ok(gone))
    ok, reason = await c2.send(ANDROID_TOKEN, msg)
    assert not ok and reason == "UNREGISTERED" and reason in DEAD_REASONS


@pytest.mark.anyio
async def test_each_device_is_pushed_over_its_own_transport():
    store = MemoryPushDeviceStore()
    wake = ["2026-09-15T11:49:30Z"]
    await store.upsert(normalize_registration({"token": TOKEN, "wakeAt": wake}, city="bogota", now=NOW))
    await store.upsert(normalize_registration({"token": ANDROID_TOKEN, "platform": "android", "wakeAt": wake},
                                              city="bogota", now=NOW))
    apns_hits, fcm_hits = [], []

    def apns_handler(req):
        apns_hits.append(req.url.path)
        return httpx.Response(200)

    def fcm_send(req):
        fcm_hits.append(json.loads(req.content)["message"]["token"])
        return httpx.Response(200, json={"name": "m1"})

    senders = {"ios": _client(apns_handler), "android": _fcm(_fcm_ok(fcm_send))}
    assert await push_wakes(store, senders, "bogota", NOW) == 2
    assert apns_hits == [f"/3/device/{TOKEN}"] and fcm_hits == [ANDROID_TOKEN]


@pytest.mark.anyio
async def test_a_device_whose_platform_has_no_transport_is_skipped_not_dropped():
    store = MemoryPushDeviceStore()
    await store.upsert(normalize_registration({"token": ANDROID_TOKEN, "platform": "android",
                                               "wakeAt": ["2026-09-15T11:49:30Z"]}, city="bogota", now=NOW))
    c = _client(lambda req: httpx.Response(200))
    assert await push_wakes(store, {"ios": c}, "bogota", NOW) == 0
    # it keeps its registration and its wake instant: the phone's own alarm is still the floor
    assert ANDROID_TOKEN in store.devices and store.devices[ANDROID_TOKEN]["wake_at"]


@pytest.mark.anyio
async def test_an_android_phone_is_told_when_the_city_cannot_reach_it(bogota: City):
    """APNs configured, FCM not: the registration is accepted so the client needs no branching, but it
    is not stored, because storing it would promise a push nothing can send."""
    city = bogota.model_copy(update={"config": bogota.config.model_copy(update={
        "push": bogota.config.push.model_copy(update={"reminders": True, "apns": bogota.config.push.apns.model_copy(
            update={"key_id": "K", "team_id": "T", "bundle_id": "b", "key_p8": KEY})})})})
    assert city.config.push.platforms() == {"ios"}
    app = _app(city)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/v1/cities/bogota/push/devices",
                        json={"token": ANDROID_TOKEN, "platform": "android"})
        assert r.status_code == 202 and r.json()["serverPush"] is False
        assert "android" in r.json()["reason"]
        assert app.state.push_devices.devices == {}


@pytest.mark.anyio
async def test_an_android_registration_is_stored_and_removed_with_its_exact_token(bogota: City):
    sa = base64.b64encode(json.dumps({
        "project_id": "opentransit-1", "client_email": "push@opentransit-1.iam.gserviceaccount.com",
        "private_key": RSA_KEY}).encode()).decode()
    city = bogota.model_copy(update={"config": bogota.config.model_copy(update={
        "push": bogota.config.push.model_copy(update={
            "reminders": True, "fcm": FcmConfig(service_account_json=sa)})})})
    assert city.config.push.platforms() == {"android"} and city.config.push.reminders_active
    app = _app(city)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/v1/cities/bogota/push/devices",
                        json={"token": ANDROID_TOKEN, "platform": "android", "routes": ["bogota:G30"]})
        assert r.status_code == 202 and r.json()["serverPush"] is True
        assert ANDROID_TOKEN in app.state.push_devices.devices
        r = await c.delete(f"/v1/cities/bogota/push/devices/{ANDROID_TOKEN}")
        assert r.status_code == 204 and app.state.push_devices.devices == {}
    # the service account never leaves the server
    assert RSA_KEY[:30] not in json.dumps(city.public())
    assert "fcm" not in city.public()["config"]["push"]
