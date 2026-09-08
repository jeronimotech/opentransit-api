"""v1.7 A2/A3/A4: shared ETA lifecycle and privacy, watch summary compactness, Live Activity endpoints."""
from __future__ import annotations

import datetime as dt
import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.analytics import RateLimiter
from app.cities import City
from app.errors import install_error_handlers
from app.forecast import ForecastCache
from app.routers import platform, share
from app.routers.watch import WatchCache, compact_rows, truncate
from app.rt import RTCache
from app.runtime import CityRuntime
from app.share import MemoryShareStore, clean_progress, expiry, hash_key, key_matches, new_token, new_write_key

ITINERARY = {"id": "it-0", "startTime": "2026-09-08T07:00:00-05:00", "endTime": "2026-09-08T07:45:00-05:00",
             "legs": [{"mode": "BUS", "transit": True, "route": {"id": "bogota:G12", "shortName": "G12"}}]}


def _app(city: City) -> tuple[FastAPI, MemoryShareStore]:
    app = FastAPI()
    install_error_handlers(app)
    for r in (platform, share):
        app.include_router(r.router)
    rt = CityRuntime(city=city, rt=RTCache(city), otp=None)  # type: ignore[arg-type]
    rt.base_city = city
    app.state.cities = {"bogota": rt}
    store = MemoryShareStore()
    app.state.share_store = store
    app.state.share_limiter = RateLimiter(30, 60)
    app.state.forecast_cache = ForecastCache()
    app.state.watch_cache = WatchCache()
    return app, store


# ------------------------------------------------------------------ pure helpers
def test_write_key_is_stored_only_as_a_digest():
    key = new_write_key()
    stored = hash_key(key)
    assert key not in stored and len(stored) == 64
    assert key_matches(key, stored)
    assert not key_matches("not-the-key", stored)
    assert not key_matches(None, stored)


def test_tokens_are_unguessable_and_distinct():
    tokens = {new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 22 for t in tokens)


def test_progress_is_coarsened_and_stripped():
    cleaned = clean_progress({"legIndex": 1, "state": "delayed", "lat": 4.684512, "lon": -74.053099,
                              "etaAt": "2026-09-08T07:45:00-05:00", "atStopId": "bogota:2000",
                              "riderName": "Luis", "deviceId": "abc"})
    assert cleaned["lat"] == 4.685 and cleaned["lon"] == -74.053     # 3 decimals ≈ 110 m
    assert set(cleaned) == {"legIndex", "state", "lat", "lon", "etaAt", "atStopId"}
    assert "riderName" not in cleaned and "deviceId" not in cleaned


def test_ttl_is_clamped_to_the_city_maximum():
    now = dt.datetime(2026, 9, 8, 7, 0, tzinfo=dt.UTC)
    assert expiry(now, 0, 180, 720) - now == dt.timedelta(minutes=180)      # default
    assert expiry(now, 60, 180, 720) - now == dt.timedelta(minutes=60)      # honoured
    assert expiry(now, 5000, 180, 720) - now == dt.timedelta(minutes=720)   # clamped


# ------------------------------------------------------------------ endpoint lifecycle
@pytest.mark.anyio
async def test_share_lifecycle_create_patch_read_revoke(bogota: City):
    app, store = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/cities/bogota/share/eta",
                         json={"itinerary": ITINERARY, "label": "Camino a casa"})
        assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
        created = r.json()
        token, key = created["token"], created["writeKey"]
        assert token in created["url"] and created["expiresAt"].endswith("Z")

        read = await c.get(f"/v1/cities/bogota/share/eta/{token}")
        assert read.status_code == 200
        body = read.json()
        assert body["label"] == "Camino a casa" and body["progress"] is None
        assert body["city"]["id"] == "bogota" and "feeds" not in body["city"]

        p = await c.patch(f"/v1/cities/bogota/share/eta/{token}",
                          headers={"X-Share-Key": key},
                          json={"progress": {"legIndex": 0, "state": "delayed",
                                             "lat": 4.684512, "lon": -74.053099}})
        assert p.status_code == 200 and p.json()["progress"]["lat"] == 4.685

        after = (await c.get(f"/v1/cities/bogota/share/eta/{token}")).json()
        assert after["progress"]["state"] == "delayed" and after["progress"]["legIndex"] == 0

        d = await c.delete(f"/v1/cities/bogota/share/eta/{token}", headers={"X-Share-Key": key})
        assert d.status_code == 204
        gone = await c.get(f"/v1/cities/bogota/share/eta/{token}")
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "SHARE_NOT_FOUND"


@pytest.mark.anyio
async def test_share_read_carries_enough_city_to_frame_the_map(bogota: City):
    """The public page must be able to paint the map without guessing from the itinerary."""
    app, _ = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        token = (await c.post("/v1/cities/bogota/share/eta", json={"itinerary": ITINERARY})).json()["token"]
        city = (await c.get(f"/v1/cities/bogota/share/eta/{token}")).json()["city"]
    assert set(city) == {"id", "name", "timezone", "center", "defaultZoom", "branding", "attribution"}
    assert city["center"] == {"lat": bogota.center.lat, "lon": bogota.center.lon}
    assert isinstance(city["defaultZoom"], int | float) and city["defaultZoom"] == bogota.default_zoom


@pytest.mark.anyio
async def test_share_read_never_exposes_the_restricted_plane(bogota: City):
    """A public link must not become a side door to feeds, credentials or the write key."""
    app, _ = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        created = (await c.post("/v1/cities/bogota/share/eta", json={"itinerary": ITINERARY})).json()
        body = (await c.get(f"/v1/cities/bogota/share/eta/{created['token']}")).json()
    # the attribution line legitimately names the data sources ("… (GTFS) · Mapa: © OpenMapTiles …"),
    # so scan everything except that one public string
    scanned = dict(body)
    scanned["city"] = {k: v for k, v in body["city"].items() if k != "attribution"}
    raw = json.dumps(scanned).lower()
    for forbidden in ("feeds", "gtfs", "rtpositionsurl", "credential", "clientid", "clientsecret",
                      "admintoken", "writekey", "keyhash", "apns", "openmobility", "mds", "providers",
                      "http://", "https://"):
        assert forbidden not in raw, forbidden
    assert created["writeKey"] not in json.dumps(body)


@pytest.mark.anyio
async def test_a_reader_cannot_move_somebody_elses_dot(bogota: City):
    app, _ = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        token = (await c.post("/v1/cities/bogota/share/eta", json={"itinerary": ITINERARY})).json()["token"]
        for headers in ({}, {"X-Share-Key": "guessed"}):
            bad = await c.patch(f"/v1/cities/bogota/share/eta/{token}", headers=headers,
                                json={"progress": {"legIndex": 1}})
            assert bad.status_code == 403 and bad.json()["error"]["code"] == "FORBIDDEN"
        bad_del = await c.delete(f"/v1/cities/bogota/share/eta/{token}", headers={"X-Share-Key": "nope"})
        assert bad_del.status_code == 403


@pytest.mark.anyio
async def test_expired_share_reads_as_gone_and_is_dropped(bogota: City):
    app, store = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        created = (await c.post("/v1/cities/bogota/share/eta", json={"itinerary": ITINERARY})).json()
        row = store.rows[("bogota", created["token"])]
        row["expires_at"] = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        assert (await c.get(f"/v1/cities/bogota/share/eta/{created['token']}")).status_code == 404
    assert await store.drop_expired() == 1 and store.rows == {}


@pytest.mark.anyio
async def test_share_rejects_bad_input(bogota: City):
    app, _ = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/v1/cities/bogota/share/eta", json={})).status_code == 422
        assert (await c.post("/v1/cities/bogota/share/eta",
                             json={"itinerary": {"legs": []}})).status_code == 422
        ok = await c.post("/v1/cities/bogota/share/eta", json={"itinerary": ITINERARY})
        token, key = ok.json()["token"], ok.json()["writeKey"]
        bad_state = await c.patch(f"/v1/cities/bogota/share/eta/{token}", headers={"X-Share-Key": key},
                                  json={"progress": {"legIndex": 0, "state": "teleporting"}})
        assert bad_state.status_code == 422
        no_leg = await c.patch(f"/v1/cities/bogota/share/eta/{token}", headers={"X-Share-Key": key},
                               json={"progress": {}})
        assert no_leg.status_code == 422


@pytest.mark.anyio
async def test_share_creation_is_rate_limited_including_rejected_attempts(bogota: City):
    """A malformed payload must not be a free way past the limiter, so every attempt counts."""
    app, _ = _app(bogota)
    app.state.share_limiter = RateLimiter(2, 60)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/v1/cities/bogota/share/eta", json={})).status_code == 422      # counts
        assert (await c.post("/v1/cities/bogota/share/eta",
                             json={"itinerary": ITINERARY})).status_code == 201               # counts
        third = await c.post("/v1/cities/bogota/share/eta", json={"itinerary": ITINERARY})
        assert third.status_code == 429 and third.json()["error"]["code"] == "RATE_LIMITED"


@pytest.mark.anyio
async def test_share_can_be_disabled_per_city(bogota: City):
    city = bogota.model_copy(deep=True)
    city.config.share.enabled = False
    app, _ = _app(city)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/cities/bogota/share/eta", json={"itinerary": ITINERARY})
        assert r.status_code == 404 and r.json()["error"]["code"] == "SHARE_DISABLED"


@pytest.mark.anyio
async def test_stored_share_never_carries_analytics_identity(bogota: City):
    """A share must not become a way to join a trip back to a session or cohort."""
    app, store = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        created = (await c.post("/v1/cities/bogota/share/eta",
                                json={"itinerary": ITINERARY, "sessionId": "sess-1234567890",
                                      "cohortId": "cohort-123456"})).json()
        await c.patch(f"/v1/cities/bogota/share/eta/{created['token']}",
                      headers={"X-Share-Key": created["writeKey"]},
                      json={"progress": {"legIndex": 0, "lat": 4.6845, "lon": -74.053}})
    blob = json.dumps(store.rows[("bogota", created["token"])], default=str)
    assert "sess-1234567890" not in blob and "cohort-123456" not in blob
    assert created["writeKey"] not in blob                    # only the digest is kept


# ------------------------------------------------------------------ A3 watch summary
def test_watch_names_are_truncated_at_a_word_boundary():
    assert truncate("Calle 100") == "Calle 100"
    long = truncate("Portal Norte - Unicervantes Terminal")
    assert len(long) <= 25 and long.endswith("…") and not long.endswith(" …")
    # no dangling separator before the ellipsis
    assert truncate("Portal Norte - Unicervantes") == "Portal Norte…"
    assert not any(truncate(n)[-2] in " -–—,;:·/" for n in ["Portal Norte - Unicervantes",
                                                            "Av. Caracas · Calle 45 Norte Bis"])
    assert truncate("Portalnortesinespaciosningunoaqui").endswith("…")


def test_watch_rows_are_capped_and_sorted_by_the_soonest():
    now = 1_757_000_000.0
    def dep(route, short, mins, realtime=False):
        return {"route": {"id": route, "shortName": short, "color": "#D22"},
                "scheduledTime": dt.datetime.fromtimestamp(now + mins * 60, dt.UTC).isoformat(),
                "realtime": realtime}
    deps = [dep("bogota:A", "A", 12), dep("bogota:B", "B", 3), dep("bogota:B", "B", 9),
            dep("bogota:C", "C", 5), dep("bogota:D", "D", 20), dep("bogota:B", "B", 15)]
    rows = compact_rows(deps, now, routes_filter=None)
    assert [r["shortName"] for r in rows] == ["B", "C", "A"]      # soonest first, capped at three routes
    assert [n["minutes"] for n in rows[0]["next"]] == [3, 9]      # at most two times per route
    only_c = compact_rows(deps, now, routes_filter={"bogota:C"})
    assert [r["shortName"] for r in only_c] == ["C"]


def test_watch_payload_stays_small():
    """Three stops with three routes each must fit comfortably in the contract's ~4 KB budget."""
    from app.models import WatchSummaryResponse
    items = [{"kind": "stop", "stopId": f"bogota:{i}", "stopName": "Portal Norte - Unicerv…",
              "component": "trunk", "distanceMeters": 120 + i,
              "routes": [{"routeId": f"bogota:R{j}", "shortName": f"R{j}", "color": "#D22020",
                          "next": [{"minutes": 3, "realtime": True}, {"minutes": 11, "realtime": False}]}
                         for j in range(3)]}
             for i in range(3)]
    body = WatchSummaryResponse(generated_at="2026-09-08T12:00:00Z",
                                freshness={"realtime": True, "ageSeconds": 12, "stale": False},
                                items=items, alerts=4).model_dump(by_alias=True)
    size = len(json.dumps(body, separators=(",", ":")))
    assert size < 4096, size
    assert "geometry" not in json.dumps(body) and "encoded" not in json.dumps(body)


# ------------------------------------------------------------------ A4 Live Activity
@pytest.mark.anyio
async def test_live_activity_endpoints_accept_and_explain_when_push_is_off(bogota: City):
    app, _ = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/cities/bogota/live-activity/register",
                         json={"activityToken": "abc123", "tripId": "trip-1", "platform": "ios"})
        assert r.status_code == 202
        assert r.json() == {"accepted": True, "serverPush": False,
                            "reason": "server push disabled; the app updates its own Live Activity"}
        assert (await c.post("/v1/cities/bogota/live-activity/register", json={})).status_code == 422
        end = await c.post("/v1/cities/bogota/live-activity/end", json={"tripId": "trip-1"})
        assert end.status_code == 202 and end.json()["serverPush"] is False


@pytest.mark.anyio
async def test_live_activity_reports_server_push_when_configured(bogota: City):
    city = bogota.model_copy(deep=True)
    city.config.push.enabled = True
    app, _ = _app(city)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/cities/bogota/live-activity/register",
                         json={"activityToken": "abc", "tripId": "t"})
        assert r.status_code == 202 and r.json()["serverPush"] is True


@pytest.mark.anyio
async def test_city_config_exposes_share_and_push_without_credentials(bogota: City):
    app, _ = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cfg = (await c.get("/v1/cities/bogota")).json()["config"]
    assert cfg["share"] == {"enabled": True, "ttlMinutes": 180}
    assert cfg["push"] == {"enabled": False}                  # no keyId / keyPath ever reaches a client


# ------------------------------------------------------------------ A3 watch summary: endpoint behaviour
# Deterministic OTP stub: Bogotá's real feed returns nothing outside service hours, which is exactly when the
# Wear OS defect was reported, so these cases are pinned with canned data instead of the live router.
def _stoptime(route_id: str, short: str, minutes_from_now: float, base_ts: float | None = None) -> dict:
    """Minutes are computed against wall-clock now, so the fixture must be anchored there too."""
    when = dt.datetime.fromtimestamp((base_ts or time.time()) + minutes_from_now * 60, dt.UTC)
    return {"scheduledDeparture": when.hour * 3600 + when.minute * 60 + when.second,
            "serviceDay": int(dt.datetime(when.year, when.month, when.day, tzinfo=dt.UTC).timestamp()),
            "realtime": False, "headsign": "Norte",
            "trip": {"gtfsId": f"bogota:{short}-{int(minutes_from_now)}",
                     "route": {"gtfsId": route_id, "shortName": short, "color": "D22020",
                               "mode": "BUS", "agency": {"gtfsId": "bogota:1", "name": "TM"}}}}


class StubOtp:
    """Answers `stop(id)` from a canned table; anything unknown behaves like OTP's empty response."""
    version = "2.9.0"

    def __init__(self, table: dict[str, list[dict]], names: dict[str, str] | None = None) -> None:
        self.table, self.names = table, names or {}
        self.calls: list[str] = []

    async def graphql(self, query, variables, locale="es"):
        sid = variables.get("id")
        self.calls.append(sid)
        if sid not in self.table:
            return {"stop": None, "station": None}
        return {"stop": {"gtfsId": sid, "name": self.names.get(sid, sid),
                         "stoptimesWithoutPatterns": self.table[sid]}}


def _watch_app(city: City, otp: StubOtp) -> FastAPI:
    from app.routers import watch as watch_router
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(watch_router.router)
    app.state.cities = {"bogota": CityRuntime(city=city, rt=RTCache(city), otp=otp)}  # type: ignore[arg-type]
    app.state.watch_cache = WatchCache()
    return app


@pytest.mark.anyio
async def test_two_requested_stops_both_come_back(bogota: City):
    """The Wear OS report: asking for two stops must yield two items, in the order asked."""
    otp = StubOtp({"bogota:2000": [_stoptime("bogota:R1", "B10", 3), _stoptime("bogota:R1", "B10", 11)],
                   "bogota:2300": [_stoptime("bogota:R2", "G12", 5)]},
                  {"bogota:2000": "Portal Norte", "bogota:2300": "Calle 100"})
    app = _watch_app(bogota, otp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/v1/cities/bogota/watch/summary?stops=bogota:2000,bogota:2300")).json()
    assert [i["stopId"] for i in body["items"]] == ["bogota:2000", "bogota:2300"]
    assert [i["stopName"] for i in body["items"]] == ["Portal Norte", "Calle 100"]
    assert [r["shortName"] for r in body["items"][0]["routes"]] == ["B10"]


@pytest.mark.anyio
async def test_a_requested_stop_with_no_departures_still_appears(bogota: City):
    """Outside service hours the watch must show "sin salidas ahora", not lose the favourite."""
    otp = StubOtp({"bogota:2000": [_stoptime("bogota:R1", "B10", 4)],
                   "bogota:2300": []},                       # last bus already gone
                  {"bogota:2000": "Portal Norte", "bogota:2300": "Calle 100"})
    app = _watch_app(bogota, otp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/v1/cities/bogota/watch/summary?stops=bogota:2000,bogota:2300")).json()
    assert [i["stopId"] for i in body["items"]] == ["bogota:2000", "bogota:2300"]
    assert body["items"][1]["routes"] == []                  # present, simply with nothing coming
    assert body["items"][1]["stopName"] == "Calle 100"


@pytest.mark.anyio
async def test_limit_never_drops_a_requested_stop_and_per_route_trims_times(bogota: City):
    otp = StubOtp({f"bogota:{n}": [_stoptime("bogota:R1", "B10", 2),
                                   _stoptime("bogota:R1", "B10", 9),
                                   _stoptime("bogota:R1", "B10", 17)] for n in (2000, 2300, 2400, 2500)},
                  {f"bogota:{n}": f"Parada {n}" for n in (2000, 2300, 2400, 2500)})
    app = _watch_app(bogota, otp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        four = (await c.get("/v1/cities/bogota/watch/summary"
                            "?stops=bogota:2000,bogota:2300,bogota:2400,bogota:2500&limit=2")).json()
        one = (await c.get("/v1/cities/bogota/watch/summary?stops=bogota:2000&perRoute=1")).json()
        three = (await c.get("/v1/cities/bogota/watch/summary?stops=bogota:2000&perRoute=3")).json()
    # `limit` bounds the nearby fill, so four explicitly requested stops all survive limit=2
    assert len(four["items"]) == 4
    assert [n["minutes"] for n in one["items"][0]["routes"][0]["next"]] == [2]
    assert [n["minutes"] for n in three["items"][0]["routes"][0]["next"]] == [2, 9, 17]


@pytest.mark.anyio
async def test_an_unknown_stop_does_not_break_the_response(bogota: City):
    otp = StubOtp({"bogota:2000": [_stoptime("bogota:R1", "B10", 6)]}, {"bogota:2000": "Portal Norte"})
    app = _watch_app(bogota, otp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/watch/summary?stops=bogota:nope,bogota:2000")
    assert r.status_code == 200
    assert [i["stopId"] for i in r.json()["items"]] == ["bogota:2000"]


@pytest.mark.anyio
async def test_one_failing_stop_does_not_sink_the_payload(bogota: City):

    class FlakyOtp(StubOtp):
        async def graphql(self, query, variables, locale="es"):
            if variables.get("id") == "bogota:2300":
                raise RuntimeError("upstream hiccup")
            return await super().graphql(query, variables, locale)

    otp = FlakyOtp({"bogota:2000": [_stoptime("bogota:R1", "B10", 6)]}, {"bogota:2000": "Portal Norte"})
    app = _watch_app(bogota, otp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/watch/summary?stops=bogota:2300,bogota:2000")
    assert r.status_code == 200
    assert [i["stopId"] for i in r.json()["items"]] == ["bogota:2000"]


# ------------------------------------------------------------------ the link a person receives
@pytest.mark.anyio
async def test_share_url_points_at_the_web_client_not_the_api(bogota: City, monkeypatch):
    """A shared trip is read by a person, and the API only speaks JSON.

    The link used to be built from the API's own base URL, so whoever received it got a
    page of raw JSON instead of the trip.
    """
    from app.config import settings

    settings.cache_clear()
    monkeypatch.setenv("WEB_BASE_URL", "https://web.example.org/")
    app, _ = _app(bogota)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.internal") as c:
            r = await c.post("/v1/cities/bogota/share/eta", json={"itinerary": ITINERARY})
            url = r.json()["url"]
            token = r.json()["token"]
        assert url == f"https://web.example.org/bogota/eta/{token}"
        assert "/v1/" not in url and "api.internal" not in url
    finally:
        settings.cache_clear()


@pytest.mark.anyio
async def test_city_config_overrides_the_shared_web_base(bogota: City, monkeypatch):
    """Each tenant deploys its own web client, so the city's own setting wins."""
    from app.config import settings

    settings.cache_clear()
    monkeypatch.setenv("WEB_BASE_URL", "https://shared.example.org")
    bogota.config.share.web_base_url = "https://viajes.bogota.gov.co"
    app, _ = _app(bogota)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.internal") as c:
            r = await c.post("/v1/cities/bogota/share/eta", json={"itinerary": ITINERARY})
        assert r.json()["url"].startswith("https://viajes.bogota.gov.co/bogota/eta/")
    finally:
        bogota.config.share.web_base_url = None
        settings.cache_clear()


@pytest.mark.anyio
async def test_unconfigured_web_base_warns_rather_than_failing(bogota: City, monkeypatch, caplog):
    """Falling back to the API link keeps old deployments working, but it is not silent:
    the link is unusable for its actual purpose."""
    from app.config import settings

    settings.cache_clear()
    # Empty, not deleted: Settings also reads .env, so unsetting the process variable
    # would not reach the unconfigured case on a developer machine.
    monkeypatch.setenv("WEB_BASE_URL", "")
    app, _ = _app(bogota)
    try:
        with caplog.at_level("WARNING", logger="ot.share"):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.internal") as c:
                r = await c.post("/v1/cities/bogota/share/eta", json={"itinerary": ITINERARY})
        assert r.status_code == 201
        assert "web base URL" in caplog.text
        assert "/v1/cities/bogota/share/eta/" in r.json()["url"]
    finally:
        settings.cache_clear()
