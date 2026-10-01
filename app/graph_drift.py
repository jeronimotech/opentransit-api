"""How far the deployed OTP graph has drifted from the static feed the API is serving.

A graph is built from one snapshot of a city's GTFS. Agencies re-issue `trip_id`s as they re-publish
their programming, and OTP matches realtime messages to its own graph: once the ids have moved, the
positions and delays still arrive but match nothing, and a rider stops seeing live times even though
every feed is up. Bogotá was at 52 % on 2026-09-30, four weeks after its graph was built, and a
rebuild took it back to 88 %, which is that feed's own ceiling.

The measurement is cheap because the API already has one side of it: `RTCache.known_trips` is the
trip-id set of the static feed it ingested today. The other side is a gzipped list of the ids the
graph was built from, published next to `graph.obj` in its release and fetched once per graph.

Nothing here changes routing. It is a number in `/health` and a warning in the log, so a rebuild
happens because something measured it rather than because someone noticed the symptom.
"""
import gzip
import logging

import httpx

log = logging.getLogger("ot.graphdrift")

# Under this, OTP is failing to match a meaningful share of realtime messages and the graph wants
# rebuilding. Chosen from the measured drift: Bogotá sits at 88 % right after a rebuild (the feed's
# own ceiling), and a week of churn has taken it as low as 54 %.
REBUILD_BELOW_PCT = 85.0
MAX_BASELINE_BYTES = 32 * 1024 * 1024


async def fetch_baseline(url: str, *, client: httpx.AsyncClient | None = None) -> set[str]:
    """The trip ids a graph was built from. The file is one id per line, gzipped."""
    cli = client or httpx.AsyncClient(timeout=60, follow_redirects=True)
    try:
        r = await cli.get(url)
        r.raise_for_status()
        raw = r.content
        if len(raw) > MAX_BASELINE_BYTES:
            raise ValueError(f"baseline is {len(raw)} bytes, more than the {MAX_BASELINE_BYTES} allowed")
        text = gzip.decompress(raw).decode() if url.endswith(".gz") or raw[:2] == b"\x1f\x8b" else raw.decode()
    finally:
        if client is None:
            await cli.aclose()
    return {line.strip() for line in text.splitlines() if line.strip()}


def overlap(baseline: set[str], current: set[str] | None) -> dict:
    """What share of the feed the API is serving the graph still recognises.

    Measured against the *current* feed, not the baseline: the question a rider feels is "can OTP
    match today's trips", so a graph that also carries thousands of retired ids is not penalised.
    """
    if not baseline or not current:
        return {"ok": None, "overlapPct": None, "graphTrips": len(baseline) or None,
                "feedTrips": len(current) if current else None, "rebuild": None}
    shared = len(baseline & current)
    pct = round(shared / len(current) * 100, 1)
    return {"ok": True, "overlapPct": pct, "graphTrips": len(baseline), "feedTrips": len(current),
            "sharedTrips": shared, "rebuild": pct < REBUILD_BELOW_PCT}


class DriftStore:
    """Last measurement per city, so `/health` never waits on a download."""

    def __init__(self) -> None:
        self._by_city: dict[str, dict] = {}
        self._baselines: dict[str, tuple[str, set[str]]] = {}   # city -> (url, ids)

    def get(self, city: str) -> dict:
        return self._by_city.get(city) or {}

    async def refresh(self, city: str, url: str | None, current: set[str] | None,
                      *, client: httpx.AsyncClient | None = None) -> dict:
        if not url:
            self._by_city[city] = {"enabled": False}
            return self._by_city[city]
        try:
            cached = self._baselines.get(city)
            if cached and cached[0] == url:
                baseline = cached[1]
            else:
                baseline = await fetch_baseline(url, client=client)
                self._baselines[city] = (url, baseline)
            out = {"enabled": True, **overlap(baseline, current), "error": None}
        except Exception as e:  # noqa: BLE001
            # A missing baseline must not colour the city unhealthy: it means nobody published one
            # for this graph yet, which is a gap in the release, not a fault in the service.
            out = {"enabled": True, "ok": False, "overlapPct": None, "rebuild": None,
                   "error": f"{type(e).__name__}: {e}"[:160]}
        if out.get("rebuild"):
            log.warning("[%s] the OTP graph recognises only %.1f%% of the feed's trips; rebuild it",
                        city, out["overlapPct"])
        self._by_city[city] = out
        return out


store = DriftStore()
