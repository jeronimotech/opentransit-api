"""v1.5 first-party analytics: privacy rules, validation, rollup idempotency, k-anonymity, export, rate limit."""
import datetime as dt
import gzip
import json
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app import geohash
from app.analytics import (
    BatchIn,
    Hasher,
    MemoryAnalyticsStore,
    RateLimiter,
    aggregate,
    bucket,
    prepare_rows,
)
from app.cities import City
from app.errors import install_error_handlers
from app.routers import analytics, platform
from app.rt import RTCache
from app.runtime import CityRuntime

H = {"X-Admin-Token": "test-token"}
T0 = dt.datetime(2026, 9, 6, 15, 7, 42, tzinfo=dt.UTC)      # 10:07 local (Bogotá)


def _batch(events: list[dict], session="sess-aaaaaaaa", cohort="coh-bbbbbbbb", platform="ios") -> dict:
    return {"sessionId": session, "cohortId": cohort, "platform": platform, "appVersion": "1.5.0",
            "locale": "es", "sentAt": T0.isoformat(), "events": events}


def _ev(etype: str, props: dict | None = None, at: dt.datetime = T0) -> dict:
    return {"type": etype, "at": at.isoformat(), "props": props or {}}


PLAN = {"fromLat": 4.684512, "fromLon": -74.053012, "toLat": 4.597812, "toLon": -74.161612,
        "fromKind": "stop", "toKind": "address", "modes": ["TRANSIT", "WALK"], "timeType": "now"}


def _app(bogota: City) -> tuple[FastAPI, CityRuntime, MemoryAnalyticsStore]:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(platform.router)
    app.include_router(analytics.router)
    rt = CityRuntime(city=bogota, rt=RTCache(bogota), otp=None)  # type: ignore[arg-type]
    app.state.cities = {"bogota": rt}
    store = MemoryAnalyticsStore()
    app.state.analytics_store = store
    app.state.analytics_hasher = Hasher()
    app.state.analytics_limiter = RateLimiter(60, 60)
    return app, rt, store


# ------------------------------------------------------------------ primitives
def test_geohash_roundtrip_and_cell_size():
    gh = geohash.encode(4.6534, -74.0836, 7)
    assert len(gh) == 7
    lat, lon = geohash.center(gh)
    assert abs(lat - 4.6534) < 0.001 and abs(lon + 74.0836) < 0.001
    a, b, c, d = geohash.bounds(gh)
    assert 0.001 < (c - a) < 0.002 and 0.001 < (d - b) < 0.002        # ~150 m cells
    ring = geohash.polygon(gh)
    assert ring[0] == ring[-1] and len(ring) == 5


def test_bucket_floors_to_five_minutes_utc():
    assert bucket(T0) == dt.datetime(2026, 9, 6, 15, 5, tzinfo=dt.UTC)
    local = dt.datetime(2026, 9, 6, 10, 9, 59, tzinfo=ZoneInfo("America/Bogota"))
    assert bucket(local) == dt.datetime(2026, 9, 6, 15, 5, tzinfo=dt.UTC)


@pytest.mark.anyio
async def test_hasher_salt_rotates_daily_and_is_not_reversible():
    h = Hasher()
    d1, d2 = dt.date(2026, 9, 6), dt.date(2026, 9, 7)
    a = await h.hash("sess-aaaaaaaa", d1)
    assert a == await h.hash("sess-aaaaaaaa", d1) and len(a) == 64
    assert a != await h.hash("sess-aaaaaaaa", d2)                    # new day, new salt
    assert "sess" not in a and await h.salt(d1) != await h.salt(d2)


def test_rate_limiter_fixed_window():
    rl = RateLimiter(3, 60)
    assert all(rl.allow("1.2.3.4", now=1000) for _ in range(3))
    assert not rl.allow("1.2.3.4", now=1001)
    assert rl.allow("5.6.7.8", now=1001)
    assert rl.allow("1.2.3.4", now=1061)                                # next window


# ------------------------------------------------------------------ validation + coarsening
@pytest.mark.anyio
async def test_prepare_rows_drops_unknown_props_and_never_stores_coordinates(bogota: City):
    batch = BatchIn.model_validate(_batch([
        _ev("plan_request", {**PLAN, "userEmail": "x@y.z", "note": "casa de Juan"}),
        _ev("search_select", {"resultType": "address", "label": "Cra 45 # 174-20", "lat": 4.75, "lon": -74.04,
                              "resultId": "should-drop"}),
        _ev("search_select", {"resultType": "station", "label": "Portal Norte", "resultId": "bogota:2000",
                              "lat": 4.7546, "lon": -74.0459}),
        _ev("favorite_add", {"kind": "place", "label": "Casa de mi novia"}),
        _ev("bogus_type", {"x": 1}),
        _ev("screen_view", {"screen": "not-a-screen"}),
    ]))
    rows, rejected = await prepare_rows(bogota, batch, Hasher(), received_at=T0)
    assert rejected == [4, 5]
    plan, addr, station, fav = rows
    assert "userEmail" not in plan["props"] and "note" not in plan["props"]
    for r in rows:
        for k in ("lat", "lon", "fromLat", "fromLon", "toLat", "toLon"):
            assert k not in r["props"]
        blob = json.dumps(r["props"])
        assert "4.68" not in blob and "-74.05" not in blob
    assert plan["from_gh7"] == geohash.encode(4.685, -74.053, 7) and len(plan["to_gh7"]) == 7
    assert addr["props"].get("label") is None and addr["props"].get("resultId") is None   # addresses: never
    assert addr["gh7"] and station["props"]["label"] == "Portal Norte"
    assert fav["props"].get("label") is None                                                # only home/work
    assert plan["at_bucket"] == dt.datetime(2026, 9, 6, 15, 5, tzinfo=dt.UTC)
    assert plan["session_hash"] != "sess-aaaaaaaa" and len(plan["session_hash"]) == 64


@pytest.mark.anyio
async def test_batch_limits_and_typed_text_rejected(bogota: City):
    with pytest.raises(ValidationError):
        BatchIn.model_validate(_batch([_ev("app_open")] * 51))
    with pytest.raises(ValidationError):
        BatchIn.model_validate({**_batch([]), "query": "what the user typed"})
    batch = BatchIn.model_validate(_batch([_ev("search_select", {"resultType": "poi", "query": "typed text",
                                                                 "label": "Parque de la 93"})]))
    rows, _ = await prepare_rows(bogota, batch, Hasher(), received_at=T0)
    assert "query" not in rows[0]["props"] and rows[0]["props"]["label"] == "Parque de la 93"
    old = BatchIn.model_validate(_batch([_ev("app_open", at=T0 - dt.timedelta(days=30))]))
    rows, rejected = await prepare_rows(bogota, old, Hasher(), received_at=T0)
    assert rows == [] and rejected == [0]


# ------------------------------------------------------------------ rollup + k-anonymity (memory store)
def _fixture_events(n_sessions: int) -> list[dict]:
    """n sessions each planning the same OD, one select, one board view; session 0 also hands off."""
    evs = []
    for i in range(n_sessions):
        evs.append((f"sess-{i:08d}", [
            _ev("app_open", {"coldStart": True}),
            _ev("screen_view", {"screen": "planner"}),
            _ev("search_select", {"resultType": "station", "resultId": "bogota:2000", "label": "Portal Norte",
                                  "lat": 4.7546, "lon": -74.0459, "field": "to"}),
            _ev("plan_request", PLAN),
            _ev("plan_result", {"count": 5, "bestDurationSeconds": 3600}),
            _ev("itinerary_select", {"index": 0, "source": "primary", "modes": ["WALK", "BUS"],
                                     "durationSeconds": 3600, "transfers": 1, "routeIds": ["bogota:12873"]}),
            _ev("board_view", {"stopId": "bogota:2000", "component": "trunk"}),
            _ev("go_start", {"durationSeconds": 3600}),
            _ev("go_end", {"completed": i % 2 == 0}),
        ]))
    evs[0][1].append(_ev("handoff", {"providerId": "taxi", "kind": "taxi", "hadEstimate": True}))
    return evs


@pytest.mark.anyio
async def test_rollup_is_idempotent_and_matches_manual_aggregation(bogota: City):
    store = MemoryAnalyticsStore()
    hasher = Hasher()
    for sid, evs in _fixture_events(6):
        rows, _ = await prepare_rows(bogota, BatchIn.model_validate(_batch(evs, session=sid)), hasher,
                                     received_at=T0)
        await store.insert(rows)
    r1 = await store.rollup(bogota)
    snap = json.dumps(store.aggs["bogota"], sort_keys=True, default=str)
    r2 = await store.rollup(bogota)
    assert r1["events"] == 55 and r2["events"] == 0
    assert json.dumps(store.aggs["bogota"], sort_keys=True, default=str) == snap      # nothing changed
    tz = ZoneInfo(bogota.timezone)
    manual = aggregate(store.rows, tz)
    funnel = manual["agg_funnel_daily"][0]
    assert funnel["day"] == dt.date(2026, 9, 6) and funnel["sessions"] == 6 and funnel["plan_requests"] == 6
    assert funnel["go_completions"] == 3
    assert manual["agg_od_hourly"][0]["n"] == 6 and manual["agg_od_hourly"][0]["hour"].hour == 10   # local hour
    assert {r["mode_set"]: r for r in manual["agg_mode_daily"]}["TRANSIT+WALK"]["requests"] == 6
    # late-arriving event for the same day recomputes the day, still idempotent
    rows, _ = await prepare_rows(bogota, BatchIn.model_validate(_batch([_ev("app_open")], session="sess-late0")),
                                 hasher, received_at=T0 + dt.timedelta(minutes=30))
    await store.insert(rows)
    assert (await store.rollup(bogota))["events"] == 1
    fun = store.aggs["bogota"]["agg_funnel_daily"]
    assert len(fun) == 1 and fun[0]["sessions"] == 7 and fun[0]["app_opens"] == 7


@pytest.mark.anyio
async def test_endpoints_apply_k_threshold_and_export(bogota: City):
    app, rt, store = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # 4 sessions: everything stays below k=5 -> suppressed; totals still count
        for sid, evs in _fixture_events(4):
            r = await c.post("/v1/cities/bogota/events", json=_batch(evs, session=sid))
            assert r.status_code == 202 and r.json()["rejected"] == []
        assert (await c.post("/v1/admin/cities/bogota/analytics/rollup", headers=H)).status_code == 200
        q = "?from=2026-09-01&to=2026-09-10"
        od = (await c.get(f"/v1/admin/cities/bogota/analytics/od{q}", headers=H)).json()
        assert od["pairs"] == [] and od["cells"]["features"] == [] and od["kThreshold"] == 5
        assert (await c.get(f"/v1/admin/cities/bogota/analytics/searches{q}", headers=H)).json()["items"] == []
        pl = await c.get(f"/v1/admin/cities/bogota/analytics/places{q}&kind=origin", headers=H)
        assert pl.json()["items"] == []
        s = (await c.get(f"/v1/admin/cities/bogota/analytics/summary{q}", headers=H)).json()
        assert s["totals"]["sessions"] == 4 and s["totals"]["planRequests"] == 4 and s["topRoutes"] == []
        # two more sessions -> 6 >= k: cells, pairs, routes, searches appear
        for sid, evs in _fixture_events(6)[4:]:
            await c.post("/v1/cities/bogota/events", json=_batch(evs, session=sid))
        await c.post("/v1/admin/cities/bogota/analytics/rollup", headers=H)
        od = (await c.get(f"/v1/admin/cities/bogota/analytics/od{q}", headers=H)).json()
        assert len(od["pairs"]) == 1 and od["pairs"][0]["n"] == 6 and od["pairs"][0]["fromCenter"]["lat"]
        assert od["cells"]["features"][0]["geometry"]["type"] == "Polygon"
        assert (await c.get(f"/v1/admin/cities/bogota/analytics/routes{q}", headers=H)).json()["items"][0] == \
            {"route_id": "bogota:12873", "views": 0, "selects": 6, "locates": 0}
        srch = (await c.get(f"/v1/admin/cities/bogota/analytics/searches{q}", headers=H)).json()["items"]
        assert srch[0]["label"] == "Portal Norte" and srch[0]["n"] == 6
        prov = (await c.get(f"/v1/admin/cities/bogota/analytics/providers{q}", headers=H)).json()["items"]
        assert prov == []                                             # 1 handoff < k
        fun = (await c.get(f"/v1/admin/cities/bogota/analytics/funnel{q}", headers=H)).json()
        assert fun["totals"]["go_completions"] == 3 and fun["days"][0]["sessions"] == 6
        hrs = (await c.get(f"/v1/admin/cities/bogota/analytics/hours{q}", headers=H)).json()
        assert hrs["planRequests"][dt.date(2026, 9, 6).weekday()][10] == 6
        csv_r = await c.get(f"/v1/admin/cities/bogota/analytics/export.csv{q}&dataset=modes", headers=H)
        assert csv_r.status_code == 200 and csv_r.headers["content-type"].startswith("text/csv")
        lines = csv_r.text.splitlines()
        assert lines[0] == "mode_set,requests,selects"
        assert "TRANSIT+WALK,6,0" in lines and "BUS+WALK,0,6" in lines      # requested vs. actually used
        assert (await c.get(f"/v1/admin/cities/bogota/analytics/summary{q}")).status_code == 401
        assert (await c.get("/v1/admin/cities/bogota/analytics/summary?from=2026-09-10&to=2026-09-01",
                            headers=H)).status_code == 422


@pytest.mark.anyio
async def test_ingest_gzip_rate_limit_and_disabled(bogota: City):
    app, rt, store = _app(bogota)
    app.state.analytics_limiter = RateLimiter(2, 60)
    body = gzip.compress(json.dumps(_batch([_ev("app_open")])).encode())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/cities/bogota/events", content=body, headers={"content-encoding": "gzip",
                                                                            "content-type": "application/json"})
        assert r.status_code == 202 and r.json()["accepted"] == 1
        r = await c.post("/v1/cities/bogota/events", json=_batch([_ev("app_open")]))
        assert r.status_code == 202
        r = await c.post("/v1/cities/bogota/events", json=_batch([_ev("app_open")]))
        assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
        big = _batch([_ev("screen_view", {"screen": "home", "pad": "x" * 2000})] * 20)
        app.state.analytics_limiter = RateLimiter(60, 60)
        r = await c.post("/v1/cities/bogota/events", json=big)
        assert r.status_code == 413
        rt.city = rt.city.model_copy(update={"config": rt.city.config.model_copy(
            update={"analytics": rt.city.config.analytics.model_copy(update={"enabled": False})})})
        r = await c.post("/v1/cities/bogota/events", json=_batch([_ev("app_open")]))
        assert r.status_code == 202 and r.json() == {"accepted": 0, "rejected": []}
        assert len(store.rows) == 2


def test_public_city_exposes_analytics_config(bogota: City):
    assert bogota.public()["config"]["analytics"] == {"enabled": True, "retentionDays": 90, "kThreshold": 5}
