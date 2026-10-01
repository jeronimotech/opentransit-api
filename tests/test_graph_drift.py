"""A graph built from one GTFS snapshot stops matching realtime once the agency re-issues its trip
ids. Nothing fails when that happens — the feeds are up, OTP is up, and riders simply stop seeing
live times — so the only way it gets noticed is by measuring it."""
import gzip

import httpx
import pytest

from app.graph_drift import REBUILD_BELOW_PCT, DriftStore, fetch_baseline, overlap


def _gz(ids: list[str]) -> bytes:
    return gzip.compress(("\n".join(ids) + "\n").encode())


def test_overlap_is_measured_against_todays_feed_not_the_graph():
    # The graph carries 2,000 retired ids on top of the 100 live ones. A rider never feels those, so
    # they must not count against it: what matters is the share of today's trips OTP can still match.
    graph = {f"t{i}" for i in range(2100)}
    feed = {f"t{i}" for i in range(2000, 2100)}
    r = overlap(graph, feed)
    assert r["overlapPct"] == 100.0 and r["rebuild"] is False
    assert r["graphTrips"] == 2100 and r["feedTrips"] == 100


def test_a_graph_that_has_fallen_behind_asks_to_be_rebuilt():
    graph = {f"t{i}" for i in range(100)}
    feed = {f"t{i}" for i in range(50, 150)}        # half the feed is new
    r = overlap(graph, feed)
    assert r["overlapPct"] == 50.0 and r["rebuild"] is True
    # and the threshold is the line between the two answers
    assert overlap(graph, {f"t{i}" for i in range(14, 114)})["rebuild"] is (86.0 < REBUILD_BELOW_PCT)


def test_nothing_is_claimed_before_the_static_feed_has_loaded():
    assert overlap({"a"}, None)["overlapPct"] is None
    assert overlap(set(), {"a"})["rebuild"] is None


@pytest.mark.anyio
async def test_the_baseline_is_read_gzipped_and_cached_per_graph():
    hits = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(200, content=_gz(["t1", "t2", "t3"]))

    cli = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ids = await fetch_baseline("https://example.test/trip-ids.txt.gz", client=cli)
    assert ids == {"t1", "t2", "t3"}

    store = DriftStore()
    url = "https://example.test/trip-ids.txt.gz"
    r = await store.refresh("bogota", url, {"t1", "t2", "t3", "t4"}, client=cli)
    assert r["overlapPct"] == 75.0 and r["rebuild"] is True
    # the same graph is not downloaded twice
    before = len(hits)
    await store.refresh("bogota", url, {"t1", "t2"}, client=cli)
    assert len(hits) == before
    # a new graph release is
    await store.refresh("bogota", url.replace("trip-ids", "trip-ids-2"), {"t1"}, client=cli)
    assert len(hits) == before + 1
    await cli.aclose()


@pytest.mark.anyio
async def test_a_missing_baseline_is_reported_without_colouring_the_city_unhealthy():
    cli = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    store = DriftStore()
    r = await store.refresh("roma", "https://example.test/missing.gz", {"t1"}, client=cli)
    assert r["enabled"] is True and r["ok"] is False and r["rebuild"] is None and r["error"]
    await cli.aclose()
    # a city that publishes no baseline simply has the check off
    assert (await store.refresh("lisboa", None, {"t1"}))["enabled"] is False
