"""Validation for rider reports — what the app got wrong, from the person who found it.

Asked for by TransMilenio against 1.16.0 (1.16): the app linked out to the operator's PQRS page and
offered no way to say "this stop is in the wrong place" or "this ramp is blocked". A report about our
own data is ours to receive.

Anonymous by construction. Nothing here reads a device id, an account or a position: a stop or route
id is present only because the rider attached one, and `contact` only if they typed it themselves.
That is the same rule the analytics pipeline follows, and it is why this cannot become a tracker.
"""

KINDS = ("wrong_info", "barrier", "other")

MAX_MESSAGE = 2000
MAX_CONTACT = 120
MAX_ID = 120
MAX_VERSION = 40


def clean_report(body: dict) -> dict:
    """The row to store, or a ValueError naming the field a rider's app got wrong."""
    kind = str(body.get("kind") or "other").strip()
    if kind not in KINDS:
        raise ValueError(f"kind: one of {', '.join(KINDS)}")
    message = str(body.get("message") or "").strip()
    if not message:
        raise ValueError("message: a description is required")
    if len(message) > MAX_MESSAGE:
        raise ValueError(f"message: at most {MAX_MESSAGE} characters")

    def opt(name: str, limit: int) -> str | None:
        v = body.get(name)
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None
        if len(v) > limit:
            raise ValueError(f"{name}: at most {limit} characters")
        return v

    return {
        "kind": kind,
        "message": message,
        "stop_id": opt("stopId", MAX_ID),
        "route_id": opt("routeId", MAX_ID),
        "app_version": opt("appVersion", MAX_VERSION),
        # Only to answer in the rider's own language; never a locale sniffed from the request.
        "locale": opt("locale", 16),
        "contact": opt("contact", MAX_CONTACT),
    }
