"""Equivalent services for one segment of a trip.

An itinerary names a single route per leg, so a rider waiting at a trunk station watches three buses
that would all have taken them where they are going go by, because the app only told them about the
fourth. This answers "what else serves this segment", meaning: which patterns call at the boarding
place and then, later in the same run, at the alighting place.

Patterns rather than routes, because a route that calls at both stops in the *other* direction is not
an alternative, and neither is one whose branch only reaches one of them. Where the pattern index is
unavailable we fall back to the route sets the static ingest learned per stop, which can only claim
that both stops are served — the response says which of the two it is, so the app can word it
honestly.
"""

import re

# A TransMilenio station is a parent with one stop per platform ("San Victorino B - 2 ó 5"), and two
# routes along the same corridor commonly board at different platforms of it. The equivalent service
# is still the one a rider wants, so the segment is resolved at station level and each alternative
# carries the platform to stand at.
MAX_ORIGIN_STOPS = 8


def stop_family(rows: list[dict], seed: str) -> set[str]:
    """`seed` plus everything that shares its station: siblings, its parent, or its children."""
    by_id = {r["stop_id"]: r for r in rows}
    s = by_id.get(seed)
    if s is None:
        return {seed}
    out = {seed}
    parent = s.get("parent_station")
    if parent:
        out.add(parent)
    for r in rows:
        if parent and r.get("parent_station") == parent:
            out.add(r["stop_id"])
        if r.get("parent_station") == seed:
            out.add(r["stop_id"])
    return out


def natural_key(s: str | None) -> tuple:
    """'B9' before 'B74': digit runs compare as numbers, so route lists read the way signage does."""
    return tuple((int(p), "") if p.isdigit() else (0, p) for p in re.findall(r"\d+|\D+", (s or "").strip()))


#: How much longer than the quickest equivalent a service may be and still count as one. Measured
#: against San Victorino, where the same stop pair is served in 5 calls by one route and in 31 by
#: another that loops through half the city first: boarding that one because it came first would
#: cost the rider the trip. Two-and-a-bit rather than tight, because a local is a real alternative
#: to an express.
DETOUR_FACTOR = 2.5
DETOUR_FLOOR = 3


def equivalent_services(patterns: list[dict], origins: set[str], destinations: set[str],
                        exclude: set[str] | None = None) -> list[dict]:
    """One entry per (route, platform) that gets a rider from `origins` to `destinations`.

    `patterns` are normalised as {route, headsign, directionId, stops:[{id,name,code}]}. A pattern
    qualifies when it calls at an origin and then, after it, at a destination — the direction check
    and the branch check are the same check. Of several patterns of one route from one platform the
    one with the fewest intermediate stops wins, so an express is not hidden behind its local.

    Two kinds of answer are then dropped, both learned from the live feed: services that reach the
    destination only after a long detour (see [DETOUR_FACTOR]), and the feed's own duplicate route
    rows — Bogotá lists "GA506" three times with different ids, and three identical chips read as a
    bug rather than as three buses.
    """
    exclude = exclude or set()
    best: dict[tuple[str, str], dict] = {}
    for p in patterns:
        route = p.get("route") or {}
        rid = route.get("id")
        stops = p.get("stops") or []
        if not rid or rid in exclude:
            continue
        board = next((i for i, s in enumerate(stops) if s["id"] in origins), None)
        if board is None:
            continue
        off = next((j for j in range(board + 1, len(stops)) if stops[j]["id"] in destinations), None)
        if off is None:
            continue
        item = {**route, "headsign": p.get("headsign"), "directionId": p.get("directionId"),
                "boardAt": stops[board], "getOffAt": stops[off], "stops": off - board}
        key = (rid, stops[board]["id"])
        if key not in best or item["stops"] < best[key]["stops"]:
            best[key] = item
    out = _running_today(list(best.values()))
    out = _drop_detours(out)
    out = _dedupe_per_platform(out)
    return sorted(out, key=lambda r: (r.get("component") or "", natural_key(r.get("shortName"))))


def _running_today(services: list[dict]) -> list[dict]:
    """Drop what does not run today.

    "Take whichever comes first" is false advice about a service that is not running. Bogotá
    publishes a second route row per route for the Sunday ciclovía — same number, long name with
    "Ciclovía" appended — and on a Thursday both appeared, which read as a duplicate chip and was
    really a bus that would never arrive. `serviceWindow.hasServiceToday` already knew.

    A service with no window at all is kept: absent is not the same as not running.
    """
    kept = []
    for s in services:
        window = s.get("serviceWindow") or {}
        if window.get("hasServiceToday") is False:
            continue
        kept.append(s)
    return kept


def _drop_detours(services: list[dict]) -> list[dict]:
    """Keep the quickest way through the segment and everything within reach of it."""
    counts = [s["stops"] for s in services if isinstance(s.get("stops"), int)]
    if not counts:
        return services
    limit = max(min(counts) * DETOUR_FACTOR, min(counts) + DETOUR_FLOOR)
    return [s for s in services if not isinstance(s.get("stops"), int) or s["stops"] <= limit]


def _dedupe_per_platform(services: list[dict]) -> list[dict]:
    """Collapse rows a rider could not tell apart, keeping the ones they could.

    Per platform rather than globally: two routes with the same name boarding at different vagones
    of one station are two different instructions, and merging them would send a rider to the wrong
    place.
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for s in services:
        # What the rider is being told: this number, from here, off there. Two feed rows that agree
        # on all three are one instruction however many ids the feed has for them — unlike the route
        # *list*, where the long name is the only thing telling two services apart, here the segment
        # is already fixed and the long name adds nothing a rider can act on.
        key = (s.get("shortName") or s.get("id"), (s.get("boardAt") or {}).get("id"),
               (s.get("getOffAt") or {}).get("id"))
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(s)
    out: list[dict] = []
    for key in order:
        group = groups[key]
        first, rest = group[0], group[1:]
        if rest:
            first = {**first, "mergedIds": [s["id"] for s in rest if s.get("id")]}
        out.append(first)
    return out
