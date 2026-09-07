"""v1.7 A2/A3/A4: shared ETA lifecycle and privacy, watch summary compactness, Live Activity endpoints."""
from __future__ import annotations

import datetime as dt
import json

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
