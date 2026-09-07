"""v1.7 A1: departure forecast — sampling, dedup, gaps, notes, recommendation and the fan-out bound."""
from __future__ import annotations

import datetime as dt

import pytest

from app.forecast import (
    MAX_FANOUT,
    ForecastCache,
    annotate_gaps,
    build_notes,
    build_options,
    mark_recommended,
    sample_times,
)

TZ = dt.UTC


def _it(depart: str, arrive: str, *, trip: str, route: str = "bogota:G12", transfers: int = 0,
        walk: float = 400.0, realtime: bool = False) -> dict:
    """A normalised itinerary reduced to what the forecast reads."""
    return {
        "startTime": depart, "endTime": arrive,
        "durationSeconds": int((dt.datetime.fromisoformat(arrive) - dt.datetime.fromisoformat(depart))
                               .total_seconds()),
        "transfers": transfers, "walkDistanceMeters": walk, "modesUsed": ["WALK", "BUS"],
        "legs": [{"mode": "BUS", "transit": True, "route": {"id": route}, "tripId": trip,
                  "from": {"stopId": "bogota:1"}, "startTime": depart, "realtime": realtime}],
    }


def test_sampling_is_bounded_and_includes_the_start():
    start = dt.datetime(2026, 9, 8, 7, 0, tzinfo=TZ)
    times = sample_times(start, 90, MAX_FANOUT)
    assert times[0] == start
    assert len(times) == MAX_FANOUT                       # never more than the contract's ceiling
    assert all(t <= start + dt.timedelta(minutes=90) for t in times)
    assert times == sorted(times)
    # a caller asking for a huge fan-out still gets the cap
    assert len(sample_times(start, 300, 99)) == MAX_FANOUT
    assert sample_times(start, 0, 5) == [start]


def test_options_dedupe_the_same_vehicle_seen_by_several_probes():
    a = _it("2026-09-08T07:05:00+00:00", "2026-09-08T07:50:00+00:00", trip="t1")
    b = _it("2026-09-08T07:20:00+00:00", "2026-09-08T08:05:00+00:00", trip="t2")
    # three probes, but only two distinct journeys: t1 shows up twice
    options = build_options([[a], [a, b], [b]], max_options=8)
    assert [o["departAt"] for o in options] == [a["startTime"], b["startTime"]]
    assert options[0]["routeIds"] == ["bogota:G12"]
    assert options[0]["walkMeters"] == 400 and "legs" not in options[0]


def test_gaps_and_long_gap_note():
    opts = build_options([[
        _it("2026-09-08T07:00:00+00:00", "2026-09-08T07:40:00+00:00", trip="t1"),
        _it("2026-09-08T07:10:00+00:00", "2026-09-08T07:50:00+00:00", trip="t2"),
        _it("2026-09-08T08:05:00+00:00", "2026-09-08T08:45:00+00:00", trip="t3"),
    ]], max_options=8)
    annotate_gaps(opts)
    assert opts[0]["gapAfterSeconds"] == 600              # 07:00 -> 07:10
    assert opts[1]["gapAfterSeconds"] == 55 * 60          # 07:10 -> 08:05, worth a warning
    assert opts[-1]["gapAfterSeconds"] is None            # nothing follows the last row
    notes = build_notes(opts, window_end=dt.datetime(2026, 9, 8, 8, 30, tzinfo=TZ), service_window=None)
    gap = [n for n in notes if n["kind"] == "long_gap"]
    assert len(gap) == 1 and "08:05" in gap[0]["text"]


def test_recommended_is_the_earliest_arrival_among_the_fastest_quartile():
    opts = build_options([[
        _it("2026-09-08T07:00:00+00:00", "2026-09-08T08:00:00+00:00", trip="slow"),   # 60 min
        _it("2026-09-08T07:15:00+00:00", "2026-09-08T07:55:00+00:00", trip="fast"),   # 40 min, arrives first
        _it("2026-09-08T07:30:00+00:00", "2026-09-08T08:10:00+00:00", trip="fast2"),  # 40 min, later
    ]], max_options=8)
    mark_recommended(opts)
    assert [o["recommended"] for o in opts] == [False, True, False]


def test_notes_flag_the_last_service_and_the_service_window_end():
    opts = build_options([[_it("2026-09-08T19:00:00+00:00", "2026-09-08T19:40:00+00:00", trip="t1")]],
                         max_options=8)
    annotate_gaps(opts)
    notes = build_notes(opts, window_end=dt.datetime(2026, 9, 8, 21, 0, tzinfo=TZ),
                        service_window={"end": "20:00", "endsNextDay": False})
    kinds = {n["kind"] for n in notes}
    assert kinds == {"last_service", "service_ends"}
    assert "20:00" in next(n for n in notes if n["kind"] == "service_ends")["text"]
    # English keeps the same structure with translated copy
    en = build_notes(opts, window_end=dt.datetime(2026, 9, 8, 21, 0, tzinfo=TZ),
                     service_window=None, locale="en")
    assert en and "Last departure" in en[0]["text"]


def test_no_options_degrades_quietly():
    assert build_options([[], []], max_options=8) == []
    opts: list[dict] = []
    annotate_gaps(opts)
    mark_recommended(opts)
    assert build_notes(opts, window_end=dt.datetime(2026, 9, 8, 9, 0, tzinfo=TZ), service_window=None) == []


def test_cache_key_rounds_nearby_taps_together_but_separates_windows():
    when = dt.datetime(2026, 9, 8, 7, 0, tzinfo=TZ)
    common = dict(when=when, window=90, modes=None, arrive_by=False, max_options=8, locale="es")
    k1 = ForecastCache.key("bogota", from_lat=4.68451, from_lon=-74.05301, to_lat=4.6, to_lon=-74.1, **common)
    k2 = ForecastCache.key("bogota", from_lat=4.68452, from_lon=-74.05302, to_lat=4.6, to_lon=-74.1, **common)
    k3 = ForecastCache.key("bogota", from_lat=4.68451, from_lon=-74.05301, to_lat=4.6, to_lon=-74.1,
                           **{**common, "window": 120})
    assert k1 == k2 and k1 != k3


def test_cache_expires():
    c = ForecastCache(ttl_s=0)
    c.put(("k",), {"options": []})
    assert c.get(("k",)) is None
    warm = ForecastCache(ttl_s=60)
    warm.put(("k",), {"options": [1]})
    assert warm.get(("k",)) == {"options": [1]}


@pytest.mark.anyio
async def test_endpoint_bounds_upstream_calls_and_caches(bogota):
    """The route must never fire more than MAX_FANOUT plans, and must serve the second call from cache."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.errors import install_error_handlers
    from app.routers import plan as plan_router
    from app.rt import RTCache
    from app.runtime import CityRuntime

    calls = {"n": 0}

    class CountingOtp:
        version = "2.9.0"

        async def graphql(self, query, variables, locale="es"):
            calls["n"] += 1
            return {"planConnection": {"edges": []}}

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(plan_router.router)
    rt = CityRuntime(city=bogota, rt=RTCache(bogota), otp=CountingOtp())  # type: ignore[arg-type]
    app.state.cities = {"bogota": rt}
    app.state.forecast_cache = ForecastCache()
    q = ("fromLat=4.6845&fromLon=-74.0530&toLat=4.5978&toLon=-74.1616"
         "&windowMinutes=180&maxOptions=12&time=2026-09-08T07:00:00-05:00")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/v1/cities/bogota/plan/forecast?{q}")
        assert r.status_code == 200
        body = r.json()
        assert body["options"] == [] and body["windowMinutes"] == 180
        assert calls["n"] == MAX_FANOUT               # a 3-hour window still costs 8 plans, not 180
        r2 = await c.get(f"/v1/cities/bogota/plan/forecast?{q}")
        assert r2.status_code == 200
        assert calls["n"] == MAX_FANOUT               # served from cache: no extra upstream work


@pytest.mark.anyio
async def test_cached_window_never_serves_another_callers_labels(bogota):
    """The window is shared; the endpoint labels are not. One user's "Casa" must not leak to the next."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.errors import install_error_handlers
    from app.routers import plan as plan_router
    from app.rt import RTCache
    from app.runtime import CityRuntime

    class StubOtp:
        version = "2.9.0"

        async def graphql(self, query, variables, locale="es"):
            return {"planConnection": {"edges": []}}

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(plan_router.router)
    app.state.cities = {"bogota": CityRuntime(city=bogota, rt=RTCache(bogota), otp=StubOtp())}  # type: ignore[arg-type]
    app.state.forecast_cache = ForecastCache()
    q = ("fromLat=4.6845&fromLon=-74.0530&toLat=4.5978&toLon=-74.1616"
         "&windowMinutes=60&time=2026-09-08T07:00:00-05:00")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        first = await c.get(f"/v1/cities/bogota/plan/forecast?{q}&fromName=Casa&toName=Oficina")
        second = await c.get(f"/v1/cities/bogota/plan/forecast?{q}&fromName=Gimnasio")
        third = await c.get(f"/v1/cities/bogota/plan/forecast?{q}")
    assert first.json()["from"]["name"] == "Casa" and first.json()["to"]["name"] == "Oficina"
    assert second.json()["from"]["name"] == "Gimnasio" and second.json()["to"]["name"] is None
    assert third.json()["from"]["name"] is None
