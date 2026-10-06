#!/usr/bin/env python3
"""Sample, for every city, how far the graph has drifted and whether riders feel it yet.

    scripts/rt_match.py                 # every city, one line each
    scripts/rt_match.py bogota roma     # just these
    scripts/rt_match.py --json          # one JSON object per line, for appending to a series

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
from datetime import datetime, timezone

DEFAULT_BASE = "https://api.opentransit.tech"
CITIES = ["bogota", "boston", "brisbane", "casablanca", "kualalumpur",
          "lisboa", "roma", "santiago", "toronto"]


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
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "city": city,
        "driftPct": g.get("overlapPct"),
        "graphTrips": g.get("graphTrips"),
        "feedTrips": g.get("feedTrips"),
        "rtByIdPct": rt.get("pctTripResolvedById"),
        "rescuedBySchedule": rt.get("tripsRescuedBySchedule"),
        "vehicles": rt.get("vehicles"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cities", nargs="*", default=None)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--json", action="store_true", help="one JSON object per line")
    a = ap.parse_args()

    rows = [sample(a.base, c) for c in (a.cities or CITIES)]
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
        print(f"{r['city']:13} {('—' if d is None else f'{d:.1f}%'):>7} "
              f"{('—' if rt is None else f'{rt:.1f}%'):>7} "
              f"{(r['vehicles'] if r['vehicles'] is not None else '—'):>9} "
              f"{(r['rescuedBySchedule'] if r['rescuedBySchedule'] is not None else '—'):>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
