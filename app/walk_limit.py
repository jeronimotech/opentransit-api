"""The walking limit as a limit, not a preference.

Asked for by TransMilenio against 1.16.0 (1.11): "distancia máxima a pie" lowered OTP's walking
reluctance, so an itinerary with a longer walk than the rider asked for still came back — fine as a
preference, useless for someone who cannot walk it.

OTP 2 has no hard walk cap, so the cap is applied here: itineraries that walk further than the rider
allowed are dropped, and the response says so. Saying so is the point — a filter that silently
empties a result list is indistinguishable from "no service", which is the failure this is meant to
prevent.
"""

WARNING = ("WALK_LIMIT_FILTERED: {dropped} of {total} options walked further than the {limit} m you "
           "allowed and were left out")


def apply_walk_limit(itineraries: list[dict], max_metres: int) -> tuple[list[dict], int]:
    """(kept, dropped). An itinerary with no walk figure is kept: absent is not the same as over."""
    kept = [it for it in itineraries if (it.get("walkDistanceMeters") or 0) <= max_metres]
    return kept, len(itineraries) - len(kept)


def walk_limit_warning(dropped: int, total: int, max_metres: int) -> list[str]:
    if dropped <= 0:
        return []
    return [WARNING.format(dropped=dropped, total=total, limit=max_metres)]
