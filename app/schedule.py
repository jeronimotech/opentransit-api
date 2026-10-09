"""The published schedule of one pattern, summarised the way a rider reads it.

Asked for by TransMilenio against 1.16.0 (1.12): the route page showed directions, stops and the
first and last departure, which answers "does it run now" and not "how long will I wait".

Two shapes, because two kinds of service are published in one feed. A trunk route runs every few
minutes and a rider wants the interval per hour; a feeder runs eleven times a day and a rider wants
the list. Both come out of the same departure times, so the client can show whichever fits without
a second request — and `frequent` says which one this pattern is.
"""

import statistics

#: A pattern is "frequent" when the typical wait, across the busiest part of the day, is at most
#: this. Twelve minutes is where a timetable stops being worth reading: below it a rider turns up
#: and waits, above it they plan around a departure.
FREQUENT_HEADWAY_MINUTES = 12


def hhmm(seconds: int) -> str:
    """GTFS seconds-since-noon-minus-12h as a clock a rider recognises; 25:10 becomes 01:10."""
    m = (seconds // 60) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def bands(departures: list[int]) -> list[dict]:
    """Per hour of service: how many departures, and the interval between them.

    The gap belongs to the hour of the *earlier* departure, which is the one a rider standing there
    at that hour experiences. The last departure of the day contributes no gap, so an hour with one
    departure and nothing after it reports a count without an interval rather than a zero wait.
    """
    deps = sorted(departures)
    by_hour: dict[int, list[int]] = {}
    gaps: dict[int, list[int]] = {}
    for i, d in enumerate(deps):
        hour = (d // 3600) % 24
        by_hour.setdefault(hour, []).append(d)
        if i + 1 < len(deps):
            gaps.setdefault(hour, []).append(deps[i + 1] - d)
    out = []
    for hour in sorted(by_hour):
        g = gaps.get(hour) or []
        mins = [round(x / 60) for x in g]
        out.append({
            "hour": hour,
            "from": f"{hour:02d}:00",
            "to": f"{(hour + 1) % 24:02d}:00",
            "trips": len(by_hour[hour]),
            "headwayMinutes": {
                "min": min(mins),
                "typical": round(statistics.median(mins)),
                "max": max(mins),
            } if mins else None,
        })
    return out


def schedule_summary(departures: list[int]) -> dict:
    """First and last departure, the count, the per-hour bands and whether it is frequent."""
    deps = sorted(departures)
    if not deps:
        return {"trips": 0, "first": None, "last": None, "frequent": False, "typicalHeadwayMinutes": None,
                "bands": []}
    b = bands(deps)
    # The median gap over the whole day, which is the wait a rider turning up at an unknown time
    # most often gets. A median rather than a mean so the one bus at 04:40, the long gap after it
    # and the last run at 23:50 do not make a five-minute trunk route look hourly — and so an
    # hourly feeder still reports its hour instead of reporting nothing.
    gaps = [round((deps[i + 1] - deps[i]) / 60) for i in range(len(deps) - 1)]
    typical = round(statistics.median(gaps)) if gaps else None
    return {
        "trips": len(deps),
        "first": hhmm(deps[0]),
        "last": hhmm(deps[-1]),
        "typicalHeadwayMinutes": typical,
        "frequent": typical is not None and typical <= FREQUENT_HEADWAY_MINUTES,
        "bands": b,
    }


#: Variants of one direction to ask OTP about. Bogotá's busiest routes publish five; the cap keeps a
#: pathological feed from turning one route page into twenty router queries.
MAX_PATTERNS_PER_DIRECTION = 8


def direction_group(patterns: list[dict], route_short_name: str | None = None,
                    wanted: str | None = None) -> list[dict]:
    """The patterns that are the same direction as [wanted] (or the main direction).

    A feed's "pattern" is a shape variant, not a direction: Bogotá publishes five near-identical
    variants per route and only some run on a given day, so a schedule read off one of them said
    "0 departures" for a route running every twenty minutes. Two variants are the same direction
    when they agree on `directionId` and on the destination sign — which is exactly what a rider
    reads on the bus and the only thing they are choosing between.

    The main direction is the one holding the longest variant: the route page opens on it.
    """
    if not patterns:
        return []

    def key(p: dict) -> tuple:
        d = p.get("directionId")
        headsign = (p.get("headsign") or "").strip().casefold()
        if not headsign and route_short_name:
            headsign = ""
        return (d if d in (0, 1) else None, headsign)

    groups: dict[tuple, list[dict]] = {}
    for p in patterns:
        groups.setdefault(key(p), []).append(p)
    if wanted:
        for g in groups.values():
            if any(p.get("code") == wanted for p in g):
                return sorted(g, key=lambda p: -len(p.get("stops") or []))
        return []
    main = max(groups.values(), key=lambda g: max(len(p.get("stops") or []) for p in g))
    return sorted(main, key=lambda p: -len(p.get("stops") or []))
