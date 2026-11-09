#!/usr/bin/env python3
"""Decide, from production's own `/health`, whether a city's graph needs a person — and which act.

    scripts/should-rebuild.py                 # every city, one verdict each
    scripts/should-rebuild.py bogota          # just this one
    scripts/should-rebuild.py --json          # one JSON object per line
    scripts/should-rebuild.py bogota --github-output   # also write verdict/city to $GITHUB_OUTPUT

This encodes the rebuild runbook so a machine can run it without re-learning the two lessons that
cost October 2026. The verdict is one of:

    ok        nothing is wrong; the graph still matches.
    hold      drift is below threshold (the static feed moved), BUT realtime still resolves by id,
              so the planner is on a slightly older network (the lesser harm) and rebuilding now
              risks nothing improving — or, as on 2026-10-06, collapsing the match from 99% to ~30%
              because realtime is still emitting the ids the current graph knows. Wait for rtById
              to fall. This is NOT an action.
    reingest  the active static feed lost a large part of the previous version's service — a partial
              publication read mid-roll (as on 2026-10-13). The fix is to re-ingest, which a redeploy
              of the `api` service does; a graph rebuild would make it worse.
    rebuild   drift is below threshold AND rtById has fallen below the floor: realtime has switched
              to the new numbering, so the graph is genuinely behind and a rebuild is both safe and
              overdue.
    unknown   /health could not be read, or reported too little to decide.

The thresholds mirror app/ops_alerts.py so the ingest alarm and this guard never disagree:
`router.graphDrift.rebuild` is the API's own below-threshold flag, and `static.shrink` is its own
partial-feed flag. The one number this adds is `--rt-floor`: how far rtById must fall before a
drifted graph is worth rebuilding. Bogotá's feed ceiling is ~88%, healthy runs sit 86–100%, and the
outages were at ~30% and 0%, so 60 is comfortably between "still fine" and "riders feel it".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

DEFAULT_BASE = "https://api.opentransit.tech"
CITIES = ["bogota", "boston", "brisbane", "casablanca", "kualalumpur",
          "lisboa", "roma", "santiago", "toronto"]

# Verdicts that a caller should act on, and the exit code each maps to with --exit-code.
ACTION_CODES = {"rebuild": 10, "reingest": 11}


def sample(base: str, city: str, timeout: float = 30) -> dict:
    url = f"{base}/v1/cities/{city}/health"
    req = urllib.request.Request(url, headers={"User-Agent": "opentransit-should-rebuild"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"__error__": f"{type(e).__name__}: {e}"}


def decide(health: dict, rt_floor: float) -> dict:
    """Map one city's /health to a verdict. Pure, so it is unit-testable without a network."""
    if health.get("__error__"):
        return {"verdict": "unknown", "why": health["__error__"]}

    st = health.get("static") or {}
    rt = health.get("realtime") or {}
    drift = (health.get("router") or {}).get("graphDrift") or {}

    drift_pct = drift.get("overlapPct")
    rt_by_id = rt.get("pctTripResolvedById")
    shrink = st.get("shrink")
    rebuild_flag = bool(drift.get("rebuild"))

    out = {"driftPct": drift_pct, "rtByIdPct": rt_by_id,
           "shrink": shrink["lostPct"] if shrink else None, "rebuildFlag": rebuild_flag}

    # 1. A partial feed is its own illness and comes first: rtById can read 0% purely because we
    #    ingested half a feed, and a rebuild from that feed would bake the damage into the graph.
    if shrink:
        out["verdict"] = "reingest"
        out["why"] = (f"active feed lost {shrink['lostPct']}% of the previous version's service "
                      f"({shrink.get('trips')} vs {shrink.get('previousTrips')} trips) — re-ingest, "
                      "do not rebuild")
        return out

    # 2. No drift problem flagged -> nothing to do.
    if not rebuild_flag:
        out["verdict"] = "ok"
        out["why"] = (f"drift {drift_pct}% is above threshold" if drift_pct is not None
                      else "no drift baseline for this city")
        return out

    # 3. Drift is below threshold. The static feed moved; whether that reaches riders depends on
    #    whether realtime has switched numbering too. That, not the drift number, is the trigger.
    if rt_by_id is None:
        # A city with no realtime feed never "feels" drift; the planner is simply a little stale.
        out["verdict"] = "hold"
        out["why"] = f"drift {drift_pct}% but no realtime feed to feel it — low priority refresh"
        return out
    if rt_by_id < rt_floor:
        out["verdict"] = "rebuild"
        out["why"] = (f"drift {drift_pct}% AND rtById {rt_by_id}% < {rt_floor}% — realtime has "
                      "moved to the new numbering; rebuild is safe and overdue")
        return out
    out["verdict"] = "hold"
    out["why"] = (f"drift {drift_pct}% but rtById {rt_by_id}% still >= {rt_floor}% — realtime has "
                  "NOT moved; rebuilding now could collapse the match (the 2026-10-06 trap). Wait.")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cities", nargs="*", help="cities to check (default: all)")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--rt-floor", type=float, default=60.0,
                    help="how far rtById must fall before a drifted graph is worth rebuilding")
    ap.add_argument("--json", action="store_true", help="one JSON object per line")
    ap.add_argument("--github-output", action="store_true",
                    help="append verdict=/city= to $GITHUB_OUTPUT (single-city runs)")
    ap.add_argument("--exit-code", action="store_true",
                    help="exit 10 if any city says rebuild, 11 if reingest (rebuild wins), else 0")
    a = ap.parse_args()

    cities = a.cities or CITIES
    rows = [{"city": c, **decide(sample(a.base, c), a.rt_floor)} for c in cities]

    if a.json:
        for r in rows:
            print(json.dumps(r, separators=(",", ":")))
    else:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        print(f"{stamp}  rt-floor={a.rt_floor:g}%")
        print(f"{'city':13} {'verdict':9} {'drift':>7} {'rtById':>7}  why")
        for r in rows:
            d = r.get("driftPct")
            rt = r.get("rtByIdPct")
            print(f"{r['city']:13} {r['verdict']:9} "
                  f"{('—' if d is None else f'{d:.1f}%'):>7} "
                  f"{('—' if rt is None else f'{rt:.1f}%'):>7}  {r.get('why','')}")

    if a.github_output and (gh := os.environ.get("GITHUB_OUTPUT")):
        # Single-city convenience for the workflow's decide job.
        r = rows[0]
        with open(gh, "a") as f:
            f.write(f"verdict={r['verdict']}\n")
            f.write(f"city={r['city']}\n")
            f.write(f"drift={r.get('driftPct')}\n")
            f.write(f"rtById={r.get('rtByIdPct')}\n")

    if a.exit_code:
        for want in ("rebuild", "reingest"):  # rebuild wins if both appear
            if any(r["verdict"] == want for r in rows):
                return ACTION_CODES[want]
    return 0


if __name__ == "__main__":
    sys.exit(main())
