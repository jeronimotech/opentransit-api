"""
v1.1 derived data that the GTFS does not publish directly:

* fare estimation from the city's flat-fare config (Maas pattern: "Tarifa estimada"),
* per-route service windows (TransMi App pattern: "Fuera de horario · próximo 04:30"),
* alert severity inference when the feed omits `severity_level`,
* the accessibility "unverified" heuristic (a constant wheelchair_boarding across the feed is a default, not data).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .cities import City

# ---------------------------------------------------------------- fares

_LABELS = {"es": ("Pasaje", "Transbordo"), "en": ("Fare", "Transfer")}


def estimate_fare(city: City, legs: list[dict], locale: str = "es") -> dict | None:
    """Flat-fare estimate: first boarding pays `base`; later boardings inside the transfer window pay
    `transfer` (up to `max_transfers` of them), anything else pays `base` again and restarts the window."""
    f = city.fares
    rentals = [lg for lg in legs if lg.get("rental") and (lg["rental"].get("priceEstimate") or {}).get("amount")]
    if f is None and not rentals:
        return None
    lbl_fare, lbl_transfer = _LABELS.get(locale, _LABELS["es"])
    transit = [lg for lg in legs if lg.get("transit")] if f is not None else []
    breakdown: list[dict] = []
    total = 0.0
    window_start: dt.datetime | None = None
    transfers_used = 0
    for lg in transit:
        t = _parse_dt(lg.get("startTime"))
        route = (lg.get("route") or {}).get("shortName")
        inside = (window_start is not None and t is not None
                  and (t - window_start).total_seconds() <= f.transfer_window_minutes * 60
                  and transfers_used < f.max_transfers)
        if inside:
            transfers_used += 1
            total += f.transfer
            breakdown.append({"label": lbl_transfer, "amount": f.transfer, "route": route, "kind": "transit"})
        else:
            window_start, transfers_used = t, 0
            total += f.base
            breakdown.append({"label": lbl_fare, "amount": f.base, "route": route, "kind": "transit"})
    # One rental pass per network per itinerary (a day pass covers both the access and the egress ride).
    charged: set[str] = set()
    currency = f.currency if f is not None else None
    for lg in rentals:
        r = lg["rental"]
        if r["networkId"] in charged:
            continue
        charged.add(r["networkId"])
        pe = r["priceEstimate"]
        total += float(pe["amount"])
        currency = currency or pe.get("currency")
        breakdown.append({"label": f"{r.get('networkName') or r['networkId']} · {pe.get('label') or 'pase'}",
                          "amount": pe["amount"], "route": None, "kind": "rental"})
    return {"amount": _num(total), "currency": currency or "COP", "estimated": True,
            "breakdown": [{**b, "amount": _num(b["amount"])} for b in breakdown]}


def _num(x: float) -> float | int:
    return int(x) if float(x).is_integer() else round(x, 2)


def _parse_dt(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- alert severity

_SEVERE = {"NO_SERVICE", "REDUCED_SERVICE", "SIGNIFICANT_DELAYS"}
_WARNING = {"DETOUR", "MODIFIED_SERVICE", "STOP_MOVED", "ACCESSIBILITY_ISSUE"}


def infer_severity(severity: str | None, effect: str | None) -> str:
    """Feed value wins; otherwise derive from the effect. Always returns INFO|WARNING|SEVERE."""
    if severity in ("INFO", "WARNING", "SEVERE"):
        return severity
    if severity == "UNKNOWN_SEVERITY" or severity is None:
        if effect in _SEVERE:
            return "SEVERE"
        if effect in _WARNING:
            return "WARNING"
        return "INFO"
    return severity


# ---------------------------------------------------------------- accessibility

def accessibility_unverified(counts: dict[int, int], threshold: float = 0.99) -> bool:
    """True when ≥ threshold of stops share one *informative* wheelchair value: a default, not a survey."""
    total = sum(counts.values())
    if total == 0:
        return False
    value, n = max(counts.items(), key=lambda kv: kv[1])
    return value != 0 and n / total >= threshold


def accessibility_block(wheelchair: str, unverified: bool, locale: str = "es") -> dict:
    if wheelchair == "unknown":
        return {"wheelchair": "unknown", "source": "none", "verified": False, "note": None}
    if unverified:
        note = ("Dato del feed no verificado (valor constante en todo el sistema)" if locale == "es"
                else "Feed value not verified (constant across the whole system)")
        return {"wheelchair": wheelchair, "source": "gtfs", "verified": False, "note": note}
    return {"wheelchair": wheelchair, "source": "gtfs", "verified": True, "note": None}


# ---------------------------------------------------------------- service windows

def hms_to_seconds(s: str) -> int | None:
    """GTFS 'H:MM:SS' (hours may exceed 24) -> seconds since service-day midnight."""
    try:
        h, m, sec = s.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec)
    except (ValueError, AttributeError):
        return None


def _hm(seconds: int) -> str:
    return f"{(seconds // 3600) % 24:02d}:{(seconds % 3600) // 60:02d}"


@dataclass
class ServiceIndex:
    """In-memory calendar for one feed version. Cheap to query per request."""
    windows: dict[str, list[tuple[str, int, int]]] = field(default_factory=dict)   # route -> [(service, first, last)]
    calendar: dict[str, tuple[tuple[int, ...], dt.date, dt.date]] = field(default_factory=dict)
    exceptions: dict[tuple[str, dt.date], int] = field(default_factory=dict)       # (service, date) -> 1|2
    flags: dict = field(default_factory=dict)

    def active_services(self, day: dt.date) -> set[str]:
        out: set[str] = set()
        for sid, (days, start, end) in self.calendar.items():
            if start <= day <= end and days[day.weekday()]:
                out.add(sid)
        for (sid, d), kind in self.exceptions.items():
            if d != day:
                continue
            if kind == 1:
                out.add(sid)
            elif kind == 2:
                out.discard(sid)
        return out

    def window_for(self, route_id: str, now: dt.datetime) -> dict | None:
        wins = self.windows.get(route_id)
        if not wins:
            return None
        today = now.date()
        yesterday = today - dt.timedelta(days=1)
        tomorrow = today + dt.timedelta(days=1)
        act_today, act_yest, act_tom = (self.active_services(d) for d in (today, yesterday, tomorrow))
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        now_s = int((now - midnight).total_seconds())
        today_w = [(f, la) for s, f, la in wins if s in act_today]
        yest_w = [(f, la) for s, f, la in wins if s in act_yest and la > 86400]
        tom_w = [(f, la) for s, f, la in wins if s in act_tom]
        active = any(f <= now_s <= la for f, la in today_w) or any(now_s + 86400 <= la for _, la in yest_w)
        if today_w:
            start, end = min(f for f, _ in today_w), max(la for _, la in today_w)
        elif yest_w and not active and not tom_w:
            start = end = None
        else:
            start = end = None
        next_start, next_day = None, None
        if not active:
            later = [f for f, _ in today_w if f > now_s]
            if later:
                next_start, next_day = _hm(min(later)), "today"
            elif tom_w:
                next_start, next_day = _hm(min(f for f, _ in tom_w)), "tomorrow"
        return {
            "start": _hm(start) if start is not None else None,
            "end": _hm(end) if end is not None else None,
            "endsNextDay": bool(end is not None and end >= 86400),
            "active": active,
            "nextStart": next_start, "nextStartDay": next_day,
            "hasServiceToday": bool(today_w),
            "source": "gtfs",
        }


def now_in(city: City) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(city.timezone))
