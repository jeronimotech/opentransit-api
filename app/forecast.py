"""
v1.7 departure forecast — "¿cuándo salir?".

`/plan` answers "how do I get there now". This answers "when should I leave", which is the question people
actually ask before a trip. It plans at intervals across a window and returns one row per genuinely distinct
departure, so the client can draw a timeline and warn about the gap after the last useful bus.

Cost control matters: a naive implementation would fire one OTP query per minute of the window. The fan-out
is capped (`MAX_FANOUT`) and every request is cached for a minute on a rounded key, so a user scrubbing the
sheet does not multiply load on the router.
"""
from __future__ import annotations

import datetime as dt
import time

MAX_FANOUT = 8                  # upstream plans per request; the contract's ceiling
LONG_GAP_SECONDS = 20 * 60      # a wait worth warning about
CACHE_TTL_S = 60
COORD_ROUND = 4                 # ~11 m: enough to share a cache entry between two taps on the same spot


def sample_times(start: dt.datetime, window_minutes: int, fanout: int) -> list[dt.datetime]:
    """Evenly spaced departure probes across the window, always including the start."""
    n = max(1, min(fanout, MAX_FANOUT))
    if n == 1 or window_minutes <= 0:
        return [start]
    step = window_minutes / n
    return [start + dt.timedelta(minutes=round(step * i)) for i in range(n)]


def _signature(it: dict) -> tuple:
    """Two options are the same journey when they use the same vehicles, not merely the same routes."""
    out = []
    for leg in it.get("legs") or []:
        out.append((leg.get("mode"), (leg.get("route") or {}).get("id"), leg.get("tripId"),
                    (leg.get("from") or {}).get("stopId"), leg.get("startTime")))
    return tuple(out)


def _iso_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def to_option(it: dict) -> dict:
    """An itinerary reduced to what a "when to leave" row needs: no legs, no geometry."""
    return {
        "departAt": it.get("startTime"),
        "arriveAt": it.get("endTime"),
        "durationSeconds": it.get("durationSeconds"),
        "transfers": it.get("transfers"),
        "walkMeters": round(it.get("walkDistanceMeters") or 0),
        "modesUsed": it.get("modesUsed") or [],
        "routeIds": [(lg.get("route") or {}).get("id") for lg in (it.get("legs") or [])
                     if lg.get("transit") and (lg.get("route") or {}).get("id")],
        "fare": it.get("fare"),
        "realtime": any(lg.get("realtime") for lg in (it.get("legs") or [])),
        "recommended": False,
        "gapAfterSeconds": None,
    }


def build_options(plans: list[list[dict]], *, max_options: int, arrive_by: bool = False) -> list[dict]:
    """Merge the sampled plans into a deduped, chronological list of departures."""
    seen: set[tuple] = set()
    options: list[dict] = []
    for itineraries in plans:
        for it in itineraries or []:
            if not (it.get("startTime") and it.get("endTime")):
                continue
            sig = _signature(it)
            if sig in seen:
                continue
            seen.add(sig)
            options.append(to_option(it))
    key = "arriveAt" if arrive_by else "departAt"
    options.sort(key=lambda o: (o.get(key) or "", o.get("durationSeconds") or 0))
    return options[:max_options]


def mark_recommended(options: list[dict]) -> None:
    """Earliest arrival among the fastest quartile: quick, but not at the cost of leaving much later."""
    timed = [o for o in options if o.get("durationSeconds")]
    if not timed:
        return
    durations = sorted(o["durationSeconds"] for o in timed)
    cutoff = durations[max(0, (len(durations) - 1) // 4)]
    pool_ = [o for o in timed if o["durationSeconds"] <= cutoff]
    best = min(pool_, key=lambda o: (_iso_ts(o.get("arriveAt")) or float("inf")))
    best["recommended"] = True


def annotate_gaps(options: list[dict]) -> None:
    """Seconds until the next departure, so the client can say "después no hay servicio hasta las 21:40"."""
    for i, opt in enumerate(options[:-1]):
        a, b = _iso_ts(opt.get("departAt")), _iso_ts(options[i + 1].get("departAt"))
        opt["gapAfterSeconds"] = int(b - a) if (a is not None and b is not None and b > a) else None
    if options:
        options[-1]["gapAfterSeconds"] = None


def build_notes(options: list[dict], *, window_end: dt.datetime, service_window: dict | None,
                locale: str = "es") -> list[dict]:
    """Human-readable warnings: long gaps, and whether service ends inside the window."""
    es = locale != "en"
    notes: list[dict] = []
    for i, opt in enumerate(options[:-1]):
        gap = opt.get("gapAfterSeconds")
        if gap and gap >= LONG_GAP_SECONDS:
            nxt = _hhmm(options[i + 1].get("departAt"))
            notes.append({"kind": "long_gap", "afterDepartAt": opt.get("departAt"), "gapSeconds": gap,
                          "text": (f"Después de esta salida no hay otra hasta las {nxt}." if es else
                                   f"After this departure the next one is at {nxt}.")})
    if options:
        last = options[-1]
        last_ts = _iso_ts(last.get("departAt"))
        if last_ts is not None and last_ts < window_end.timestamp() - LONG_GAP_SECONDS:
            hhmm = _hhmm(last.get("departAt"))
            notes.append({"kind": "last_service", "atDepartAt": last.get("departAt"),
                          "text": (f"Última salida encontrada en esta ventana: {hhmm}." if es else
                                   f"Last departure found in this window: {hhmm}.")})
    if service_window and service_window.get("end") and not service_window.get("endsNextDay"):
        end = service_window["end"]
        if _hhmm_before(end, window_end):
            notes.append({"kind": "service_ends", "at": end,
                          "text": (f"El servicio termina a las {end}." if es else
                                   f"Service ends at {end}.")})
    return notes


def _hhmm(iso_value: str | None) -> str:
    if not iso_value:
        return "--:--"
    try:
        return dt.datetime.fromisoformat(iso_value.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return "--:--"


def _hhmm_before(hhmm: str, moment: dt.datetime) -> bool:
    try:
        h, m = (int(x) for x in hhmm.split(":")[:2])
    except (ValueError, TypeError):
        return False
    return (h, m) <= (moment.hour, moment.minute)


class ForecastCache:
    """Tiny TTL cache keyed on the rounded query, so scrubbing the sheet does not hammer OTP."""

    def __init__(self, ttl_s: int = CACHE_TTL_S, max_entries: int = 256) -> None:
        self.ttl, self.max_entries = ttl_s, max_entries
        self._items: dict[tuple, tuple[float, dict]] = {}

    @staticmethod
    def key(city_id: str, *, from_lat: float, from_lon: float, to_lat: float, to_lon: float,
            when: dt.datetime, window: int, modes: str | None, arrive_by: bool, max_options: int,
            locale: str) -> tuple:
        return (city_id, round(from_lat, COORD_ROUND), round(from_lon, COORD_ROUND),
                round(to_lat, COORD_ROUND), round(to_lon, COORD_ROUND),
                int(when.timestamp() // 60), window, modes or "", arrive_by, max_options, locale)

    def get(self, key: tuple) -> dict | None:
        hit = self._items.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        self._items.pop(key, None)
        return None

    def put(self, key: tuple, value: dict) -> None:
        if len(self._items) >= self.max_entries:
            oldest = min(self._items, key=lambda k: self._items[k][0])
            self._items.pop(oldest, None)
        self._items[key] = (time.time(), value)
