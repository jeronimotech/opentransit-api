"""
Regulated taximeter tariff engine (v1.4).

A city configures one or more `TaxiTariff`s (flag fall, price per distance/time unit, minimum fare, surcharges,
zones). Given a car route (distance + duration) and a local departure time, `estimate()` returns an honest
price band with a breakdown. Nothing here knows about any city or provider: rules come from the config.
"""
from __future__ import annotations

import datetime as dt
import math
from functools import lru_cache

from .cities import TaxiSurcharge, TaxiTariff

_LABELS = {
    "es": {"ride": "Carrera ({km} km)", "minimum": "Carrera mínima",
           "band": "Estimación ±{pct} % · el taxímetro manda"},
    "en": {"ride": "Ride ({km} km)", "minimum": "Minimum fare", "band": "Estimate ±{pct} % · the meter decides"},
}


def _labels(locale: str) -> dict:
    return _LABELS["en"] if (locale or "es").startswith("en") else _LABELS["es"]


@lru_cache(maxsize=64)
def _holiday_table(country: str, year: int):
    """Public holidays per country/year (python-holidays). Unknown country -> empty set, never an error."""
    try:
        import holidays
        return holidays.country_holidays(country.upper(), years=year)
    except Exception:  # noqa: BLE001  (unsupported country code, missing package...)
        return {}


def is_holiday(country: str | None, day: dt.date) -> bool:
    if not country:
        return False
    return day in _holiday_table(country, day.year)


def point_in_polygon(lon: float, lat: float, polygon: list[list[float]]) -> bool:
    """Ray casting on [[lon, lat], ...]. Robust to an unclosed ring."""
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if (yi > lat) != (yj > lat):
            x = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x:
                inside = not inside
        j = i
    return inside


def _hm(s: str | None) -> int | None:
    if not s:
        return None
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _in_night_window(when: dt.datetime, start: str | None, end: str | None) -> bool:
    a, b = _hm(start), _hm(end)
    if a is None or b is None:
        return False
    t = when.hour * 60 + when.minute
    return (a <= t < b) if a <= b else (t >= a or t < b)      # b < a wraps midnight


def zones_touched(tariff: TaxiTariff, points: list[tuple[float, float]]) -> set[str]:
    """Ids of tariff zones containing any of the (lat, lon) points."""
    out: set[str] = set()
    pts = [(lat, lon) for lat, lon in points if lat is not None and lon is not None]
    for z in tariff.zones:
        if any(point_in_polygon(lon, lat, z.polygon) for lat, lon in pts):
            out.add(z.id)
    return out


def applicable_surcharges(tariff: TaxiTariff, when: dt.datetime, *, country: str | None,
                          points: list[tuple[float, float]], optional_ids: set[str] | None = None
                          ) -> list[TaxiSurcharge]:
    """Surcharges that apply to a trip starting at `when` (local time) touching `points` (lat, lon)."""
    touched = zones_touched(tariff, points) if any(s.when.zones for s in tariff.surcharges) else set()
    wanted = optional_ids or set()
    out: list[TaxiSurcharge] = []
    for s in tariff.surcharges:
        w = s.when
        if w.optional:
            if s.id in wanted:
                out.append(s)
            continue
        hit = False
        if w.night_from and w.night_to and _in_night_window(when, w.night_from, w.night_to):
            hit = True
        if w.sundays and when.weekday() == 6:
            hit = True
        if w.holidays and is_holiday(country, when.date()):
            hit = True
        if w.zones and touched.intersection(w.zones):
            hit = True
        if hit:
            out.append(s)
    return out


def _round(x: float, unit: int) -> float:
    return float(int(round(x / unit)) * unit) if unit > 1 else round(x, 2)


def estimate(tariff: TaxiTariff, distance_m: float, duration_s: float, when: dt.datetime, *,
             country: str | None = None, points: list[tuple[float, float]] | None = None,
             optional_ids: set[str] | None = None, locale: str = "es") -> dict:
    """Price band for one ride. `when` must be a timezone-aware local datetime."""
    lb = _labels(locale)
    dist_units = math.ceil(max(distance_m, 0) / tariff.unit_meters)
    waiting_s = max(duration_s, 0) * tariff.waiting_share
    wait_units = int(waiting_s // tariff.unit_seconds)
    ride = tariff.flag_fall + (dist_units + wait_units) * tariff.unit_price
    minimum_applied = ride < tariff.minimum_fare
    ride = max(ride, tariff.minimum_fare)
    ride = _round(ride, tariff.rounding)
    km = f"{distance_m / 1000:.1f}".rstrip("0").rstrip(".")
    breakdown = [{"label": lb["minimum"] if minimum_applied else lb["ride"].format(km=km), "amount": ride}]
    applied = applicable_surcharges(tariff, when, country=country, points=points or [], optional_ids=optional_ids)
    total = ride
    for s in applied:
        total += s.amount
        breakdown.append({"label": s.label, "amount": _round(s.amount, tariff.rounding)})
    total = _round(total, tariff.rounding)
    band = tariff.band_pct
    return {
        "amount": total, "min": _round(total * (1 - band), tariff.rounding),
        "max": _round(total * (1 + band), tariff.rounding), "currency": tariff.currency, "estimated": True,
        "breakdown": breakdown, "surchargesApplied": [s.id for s in applied],
        "note": lb["band"].format(pct=int(round(band * 100))),
        "tariffId": tariff.id,
    }
