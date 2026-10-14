"""Did the feed we just ingested lose a large part of its service?

Measured 2026-10-14: TransMilenio was mid-publication when our 24-hourly refresh read its feed, so
we stored 110,648 trips where the finished feed has 179,919. Nothing failed — the ingest was valid,
the router was healthy, every check was green — and because the trip ids in that partial snapshot
appear nowhere in the realtime feed, Bogotá lost live arrivals for a day. A rider saw a map of buses
that never moved.

A feed may legitimately shrink: a city cuts service, an agency splits its dataset, a seasonal
calendar ends. So this never refuses an ingest. It states the drop, loudly enough to be seen in the
morning instead of a day later.
"""

#: How much of the previous version's service may disappear before we say so. A third is far outside
#: what a real timetable change does between two consecutive publications of the same feed, and well
#: clear of the ±14 % day-to-day churn measured on this feed's trip ids.
SHRINK_THRESHOLD_PCT = 30.0


def feed_shrink(previous_trips: int | None, trips: int | None,
                threshold_pct: float = SHRINK_THRESHOLD_PCT) -> dict | None:
    """The drop worth reporting, or None.

    `previous_trips` is the version this one replaced. None on a first ingest — nothing to compare
    against is not a problem, and a city whose feed genuinely has no trips is a different alarm.
    """
    if not previous_trips or not trips or trips >= previous_trips:
        return None
    lost_pct = 100.0 * (previous_trips - trips) / previous_trips
    if lost_pct < threshold_pct:
        return None
    return {
        "previousTrips": previous_trips,
        "trips": trips,
        "lostPct": round(lost_pct, 1),
        "note": (f"this feed version carries {trips:,} trips where the previous one carried "
                 f"{previous_trips:,} ({round(lost_pct, 1)} % fewer). A partial publication reads "
                 f"exactly like this and costs live arrivals until the next ingest."),
    }
