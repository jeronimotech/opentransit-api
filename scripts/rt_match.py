#!/usr/bin/env python3
"""Sample, for every city, how far the graph has drifted and whether riders feel it yet.

    scripts/rt_match.py                 # every city, one line each
    scripts/rt_match.py bogota roma     # just these
    scripts/rt_match.py --json          # one JSON object per line, for appending to a series
    scripts/rt_match.py --fail-on-stale # exit 1 when a positions feed has frozen

Two numbers that are easy to confuse:

- `drift` is the graph measured against the **whole static feed**, including service dates in the
  future. It moves the moment an agency republishes its programming.
- `rtById` is the share of **live vehicles right now** whose trip OTP can find by id. That is the
  one a rider feels: below it, the map stops showing buses moving and arrivals fall back to the
  timetable.

They come apart, and the gap is the point. On 2026-10-05 Bogotá and Roma republished and drift
fell to 69.7 % and 64.7 % within a day of a rebuild, while `rtById` stayed at 95.9 % and 100 %:
the realtime feed was still emitting the ids the graph knew. Drift is the early warning; rtById is
the deadline. This exists to measure the lag between them, so the rebuild cadence can be chosen
from data instead of from the first number that looks alarming.

Everything is read from the deployed API's `/health`, which already computes both, so this costs
one small request per city and measures what production actually serves rather than what a fresh
checkout would.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

DEFAULT_BASE = "https://api.opentransit.tech"
CITIES = ["bogota", "boston", "brisbane", "casablanca", "kualalumpur",
          "lisboa", "roma", "santiago", "toronto"]

# A frozen upstream feed is the one outage nothing else here catches. It keeps answering 200 with a
# full fleet, but its timestamps stop advancing, so overlap stays 100 % and rtById stays high on
# fossil positions — the drift check and the shrink check both read healthy while riders watch buses
# that stopped moving hours ago. TransMilenio froze this way on 2026-11-26 (12.7 h of vehicles dated
# the night before), and a person noticed before any check did. The tell is entity age: healthy
# positions are tens of seconds old, a freeze grows without bound. The overnight trap (the Roma
# lesson, 2026-11-23) is avoided WITHOUT wall-clock service hours, because a frozen feed KEEPS its
# fleet while a feed that is merely asleep EMPTIES — so a stale age is only called frozen when the
# feed is still carrying a fleet. The same age/fleet pair that tells should-rebuild.py "not drift,
# don't rebuild" tells this check "but say so" — deliberately complementary, not contradictory.
STALE_MAX_AGE = 900.0      # entity p50 age (s) above which a feed still carrying a fleet is frozen
STALE_MIN_VEHICLES = 10    # ...and "a fleet" is at least this many vehicles, so a quiet night is not stale


def sample(base: str, city: str, timeout: float = 30) -> dict:
    url = f"{base}/v1/cities/{city}/health"
    req = urllib.request.Request(url, headers={"User-Agent": "opentransit-rt-match"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"city": city, "error": f"{type(e).__name__}: {e}"}
    rt = d.get("realtime") or {}
    g = (d.get("router") or {}).get("graphDrift") or {}
    st = d.get("static") or {}
    return {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "city": city,
        "shrink": st.get("shrink"),
        "driftPct": g.get("overlapPct"),
        "graphTrips": g.get("graphTrips"),
        "feedTrips": g.get("feedTrips"),
        "rtByIdPct": rt.get("pctTripResolvedById"),
        "rescuedBySchedule": rt.get("tripsRescuedBySchedule"),
        "vehicles": rt.get("vehicles"),
        "ageP50": rt.get("entityAgeP50Seconds"),
    }


def is_stale(row: dict, max_age: float = STALE_MAX_AGE, min_vehicles: int = STALE_MIN_VEHICLES) -> bool:
    """Has this city's positions feed frozen — still carrying a fleet, but on stale timestamps?

    Read frozen only when the feed BOTH is old (p50 entity age > max_age) AND still reports a fleet
    (vehicles >= min_vehicles). The first half is the freeze; the second half is what keeps a feed
    that is legitimately empty overnight — Casablanca always, Roma at 3am — from reading stale. A
    city with realtime disabled, an unreadable /health, or no age reported says nothing here.
    """
    if row.get("error"):
        return False
    age = row.get("ageP50")
    vehicles = row.get("vehicles")
    if age is None or vehicles is None:
        return False
    return age > max_age and vehicles >= min_vehicles


def confirm_stale(base: str, city: str, max_age: float = STALE_MAX_AGE,
                  min_vehicles: int = STALE_MIN_VEHICLES, samples: int = 2,
                  interval: float = 15.0) -> bool:
    """Re-read a would-be-frozen city before failing on it. A freeze is persistent and its p50 age
    only grows, so every read stays stale; this just guards against a single fluke sample. The alarm
    costs nothing to delay, and the 2026-11-23 Roma lesson is that no single read should redden it.
    """
    for i in range(max(1, samples)):
        if i:
            time.sleep(interval)
        if not is_stale(sample(base, city), max_age, min_vehicles):
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cities", nargs="*", default=None)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--json", action="store_true", help="one JSON object per line")
    ap.add_argument("--fail-on-shrink", action="store_true",
                    help="exit 1 when a city's active feed lost a large part of the previous one's "
                         "service — a partial publication, which costs live arrivals silently")
    ap.add_argument("--fail-on-stale", action="store_true",
                    help="exit 1 when a positions feed is frozen — still serving a full fleet but on "
                         "timestamps that stopped advancing (the 2026-11-26 TransMilenio outage, "
                         "which the drift and shrink checks cannot see)")
    ap.add_argument("--max-age", type=float, default=STALE_MAX_AGE,
                    help=f"entity p50 age (s) above which a feed still carrying a fleet is frozen "
                         f"(default {STALE_MAX_AGE:g})")
    ap.add_argument("--min-vehicles", type=int, default=STALE_MIN_VEHICLES,
                    help="vehicle count at or above which an old feed is read as frozen rather than "
                         f"asleep for the night (default {STALE_MIN_VEHICLES})")
    ap.add_argument("--stale-samples", type=int, default=2,
                    help="reads used to confirm a frozen feed before failing (default 2)")
    ap.add_argument("--stale-interval", type=float, default=15.0,
                    help="seconds between those confirmation reads (default 15)")
    a = ap.parse_args()

    cities = a.cities or CITIES
    rows = [sample(a.base, c) for c in cities]
    if a.json:
        for r in rows:
            print(json.dumps(r, separators=(",", ":")))
        return 0

    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    print(f"{stamp}   drift = graph vs whole static feed · rtById = live vehicles matched by id")
    print(f"{'city':13} {'drift':>7} {'rtById':>7} {'vehicles':>9} {'rescued':>8}")
    for r in rows:
        if r.get("error"):
            print(f"{r['city']:13} {r['error']}")
            continue
        d = r["driftPct"]
        rt = r["rtByIdPct"]
        # A city with no realtime feed reports nothing here; that is not a gap, it is Casablanca.
        marks = ""
        if r.get("shrink"):
            marks += f"  FEED SHRANK {r['shrink']['lostPct']}%"
        if is_stale(r, a.max_age, a.min_vehicles):
            marks += f"  FEED STALE {int(r['ageP50'])}s"
        print(f"{r['city']:13} {('—' if d is None else f'{d:.1f}%'):>7} "
              f"{('—' if rt is None else f'{rt:.1f}%'):>7} "
              f"{(r['vehicles'] if r['vehicles'] is not None else '—'):>9} "
              f"{(r['rescuedBySchedule'] if r['rescuedBySchedule'] is not None else '—'):>8}{marks}")

    shrunk = [r for r in rows if r.get("shrink")]
    for r in shrunk:
        print(f"\n{r['city']}: {r['shrink']['note']}")

    stale = [r for r in rows if is_stale(r, a.max_age, a.min_vehicles)]
    for r in stale:
        age = int(r["ageP50"])
        print(f"\n{r['city']}: positions feed frozen — {age}s (~{age // 3600}h) old with "
              f"{r['vehicles']} vehicles still reporting. The agency's feed stopped advancing, so "
              f"riders see fossil positions. Not a rebuild: the graph is fine, the feed is upstream.")

    rc = 0
    if a.fail_on_shrink and shrunk:
        rc = 1
    if a.fail_on_stale and stale:
        confirmed = [r["city"] for r in stale
                     if confirm_stale(a.base, r["city"], a.max_age, a.min_vehicles,
                                      a.stale_samples, a.stale_interval)]
        if confirmed:
            print(f"\nstill frozen after {a.stale_samples} reads: {', '.join(confirmed)}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
