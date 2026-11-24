#!/usr/bin/env python3
"""Measure, and optionally gate on, how far a city's deployed OTP graph has drifted from its feed.

    scripts/graph_drift.py bogota                 # print the overlap
    scripts/graph_drift.py --all                  # every city that publishes a baseline
    scripts/graph_drift.py bogota --threshold 85  # exit 1 when it has fallen below
    scripts/graph_drift.py --all --check drift    # ignore the calendar warning
    scripts/graph_drift.py --all --check calendar # only the calendar warning

Also writes the baseline a future run compares against:

    scripts/graph_drift.py --write-baseline data/bogota/bogota-gtfs.zip data/bogota/trip-ids.txt.gz

Why this exists: a graph is built from one snapshot of a GTFS, and agencies re-issue `trip_id`s as
they re-publish their programming. OTP matches realtime messages against its own graph, so once the
ids have moved the positions keep arriving and match nothing — riders lose live times while every
feed reports healthy. Measured on Bogotá, 2026-09-30: a graph four weeks old recognised 52 % of the
feed's trips, and a rebuild took it to 88 %, which is that feed's own ceiling.

Reading the feed is kept cheap. Only `trips.txt` is needed, so when the host supports range requests
the zip's central directory is read first and just that member is pulled: a few MB instead of the
120 MB of Bogotá's full feed. Hosts that refuse ranges are downloaded whole, streamed to a temp file.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UA = {"User-Agent": "opentransit-graph-drift/1.0 (+https://opentransit.tech)"}
DEFAULT_THRESHOLD = 85.0
DEFAULT_BASE = "https://api.opentransit.tech"
# The overlap number can't see the one thing that decides whether a drop is ours to fix today:
# has realtime moved to the new numbering? These mirror should-rebuild.py so this alarm and the
# automation never disagree. A drifted graph is worth rebuilding TODAY only when rtById has fallen
# below RT_FLOOR on a live positions feed. At or above it, realtime still speaks the old graph's ids
# and a rebuild would collapse the match (the 2026-10-06 trap); on a stale or empty feed a low
# rtById is an upstream outage, not drift (the 2026-11-19 lesson).
RT_FLOOR = 60.0
RT_MAX_AGE = 1800.0
RT_MIN_VEHICLES = 10
# How close the feed's last service date may come before it is worth saying so. A month is enough
# notice to notice, and short enough that it is not shouting all year.
CALENDAR_WARN_DAYS = 45


# ----------------------------------------------------------------- reading one member of a remote zip
class _RangeFile(io.RawIOBase):
    """A seekable file over HTTP range requests, so `zipfile` can read one member of a remote zip."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.pos = 0
        # One byte, and believe only a 206 with a usable Content-Range. Headers lie in both
        # directions: TransMilenio sends no Content-Length on HEAD and answers 403 to a Range, while
        # Roma Mobilità ignores the Range and returns all 47 MB with a 200.
        req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            if getattr(r, "status", r.getcode()) != 206:
                raise OSError("the host does not honour range requests")
            m = re.match(r"bytes \d+-\d+/(\d+)$", r.headers.get("content-range", "").strip())
            if not m:
                raise OSError("the host sent no usable Content-Range")
            self.size = int(m.group(1))

    def seek(self, off: int, whence: int = 0) -> int:
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def tell(self) -> int:
        return self.pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # noqa: ANN001
        if self.pos >= self.size or len(b) == 0:
            return 0
        hi = min(self.pos + len(b), self.size) - 1
        req = urllib.request.Request(self.url, headers={**UA, "Range": f"bytes={self.pos}-{hi}"})
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read(len(b))
        b[: len(data)] = data
        self.pos += len(data)
        return len(data)


def _open_remote_zip(url: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BufferedReader(_RangeFile(url), 1 << 18))
    except (OSError, zipfile.BadZipFile, KeyError):
        # No ranges, or a host that lies about them: fall back to the whole file.
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=600) as r:
            while chunk := r.read(1 << 20):
                tmp.write(chunk)
        tmp.close()
        return zipfile.ZipFile(tmp.name)


def _reader(z: zipfile.ZipFile, name: str):
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig", newline=""))


def calendar_end(z: zipfile.ZipFile) -> str | None:
    """The last date the feed has any service for. A graph is useless past it, and the feeds do not
    warn: TransMilenio's calendar runs to 2026-12-31, so every graph built before a January
    republication loses its whole timetable on New Year's Day while reporting healthy."""
    ends = []
    for name in ("calendar.txt", "calendar_dates.txt"):
        if name not in z.namelist():
            continue
        for r in _reader(z, name):
            v = (r.get("end_date") or r.get("date") or "").strip()
            if len(v) == 8 and v.isdigit() and (name == "calendar.txt" or r.get("exception_type") == "1"):
                ends.append(v)
    return max(ends) if ends else None


def trip_ids_from_zip(z: zipfile.ZipFile) -> set[str]:
    name = next((n for n in z.namelist() if n.rsplit("/", 1)[-1] == "trips.txt"), None)
    if name is None:
        raise ValueError("the feed has no trips.txt")
    with z.open(name) as f:
        return {r["trip_id"] for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")) if r.get("trip_id")}


def read_baseline(src: str) -> set[str]:
    if re.match(r"^https?://", src):
        req = urllib.request.Request(src, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
    else:
        raw = Path(src).read_bytes()
    text = gzip.decompress(raw).decode() if raw[:2] == b"\x1f\x8b" else raw.decode()
    return {line.strip() for line in text.splitlines() if line.strip()}


def write_baseline(gtfs: str, out: str) -> int:
    z = zipfile.ZipFile(gtfs) if not re.match(r"^https?://", gtfs) else _open_remote_zip(gtfs)
    ids = sorted(trip_ids_from_zip(z))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        f.write("\n".join(ids) + "\n")
    return len(ids)


# ----------------------------------------------------------------- the check
def check(city_id: str, threshold: float) -> dict:
    from app.cities import load_city_file

    city = load_city_file(ROOT / "cities" / f"{city_id}.yaml")
    baseline_url = city.otp.trip_ids_url
    if not baseline_url:
        return {"city": city_id, "skipped": "no otp.trip_ids_url in the city file"}
    feed_url = city.feeds.gtfs_static_url
    if not feed_url:
        return {"city": city_id, "skipped": "the city has no static feed"}

    baseline = read_baseline(baseline_url)
    z = _open_remote_zip(feed_url)
    current = trip_ids_from_zip(z)
    shared = len(baseline & current)
    pct = shared / len(current) * 100 if current else 0.0
    ends = calendar_end(z)
    days_left = None
    if ends:
        end = dt.date(int(ends[:4]), int(ends[4:6]), int(ends[6:]))
        days_left = (end - dt.date.today()).days
    return {"city": city_id, "graphTrips": len(baseline), "feedTrips": len(current),
            "sharedTrips": shared, "overlapPct": round(pct, 1), "rebuild": pct < threshold,
            "calendarEnds": ends, "calendarDaysLeft": days_left,
            "calendarWarning": days_left is not None and days_left <= CALENDAR_WARN_DAYS}


# ----------------------------------------------- is a measured drift actionable *today*?
def fetch_health(base: str, city_id: str, timeout: float = 30) -> dict:
    """The API's own /health for a city; {'__error__': ...} if it can't be read."""
    url = f"{base}/v1/cities/{city_id}/health"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"__error__": f"{type(e).__name__}: {e}"}


def realtime_signal(health: dict, rt_floor: float = RT_FLOOR,
                    rt_max_age: float = RT_MAX_AGE, rt_min_vehicles: int = RT_MIN_VEHICLES) -> str:
    """Does realtime confirm the graph has actually fallen behind? Mirrors should-rebuild.py.

        moved     rtById below the floor on a live feed — realtime switched to the new numbering, so
                  the graph is genuinely behind and a rebuild is the fix. Actionable today.
        steady    rtById still at/above the floor — realtime has NOT moved; rebuilding now could
                  collapse the match (the 2026-10-06 trap). Hold, don't alarm.
        feeddown  the positions feed is stale (median age > rt_max_age) or nearly empty
                  (< rt_min_vehicles) — a low rtById then measures a dead feed, not drift (the
                  2026-11-19 lesson). The feed's problem, not the graph's.
        unknown   no rtById, or /health unreadable — can't tell, so the caller fails loud.
    """
    if health.get("__error__"):
        return "unknown"
    rt = health.get("realtime") or {}
    rt_by_id = rt.get("pctTripResolvedById")
    rt_age = rt.get("entityAgeP50Seconds")
    rt_vehicles = rt.get("vehicles")
    if rt_by_id is None:
        return "unknown"
    if (rt_age is not None and rt_age > rt_max_age) or \
       (rt_vehicles is not None and rt_vehicles < rt_min_vehicles):
        return "feeddown"
    return "moved" if rt_by_id < rt_floor else "steady"


def consolidate_realtime(samples: list[dict]) -> dict:
    """Best-case realtime across repeated /health reads — mirrors should-rebuild.py:consolidate().

    rtById is measured over whoever is reporting that instant, so on a small fleet it swings (Roma's
    ~200-vehicle feed read 33% then 97% minutes apart on 2026-11-23). A graph that has really fallen
    behind reads low on every sample; noise only dips on some. So a 'moved' signal is judged by the
    best rtById across the samples before it fails the daily alarm.
    """
    readable = [s for s in samples if not s.get("__error__")]
    if not readable:
        return samples[-1] if samples else {"__error__": "no samples"}
    rts = [s.get("realtime") or {} for s in readable]

    def across(key, fn):
        vals = [rt.get(key) for rt in rts if rt.get(key) is not None]
        return fn(vals) if vals else None

    merged = dict(readable[-1])
    merged["realtime"] = {**(merged.get("realtime") or {}),
                          "pctTripResolvedById": across("pctTripResolvedById", max),
                          "vehicles": across("vehicles", max),
                          "entityAgeP50Seconds": across("entityAgeP50Seconds", min)}
    return merged


def confirmed_signal(base: str, city_id: str, rt_floor: float, rt_max_age: float,
                     rt_min_vehicles: int, samples: int = 3, interval: float = 20.0) -> str:
    """realtime_signal for a live city, re-sampling a 'moved' read to rule out fleet noise.

    Only 'moved' sends the alarm red, so it is the only signal worth confirming; every other read
    returns on the first fetch. A dip that recovers across the samples reads as 'steady' and holds.
    """
    first = fetch_health(base, city_id)
    sig = realtime_signal(first, rt_floor, rt_max_age, rt_min_vehicles)
    if sig != "moved" or samples <= 1:
        return sig
    reads = [first]
    for _ in range(samples - 1):
        time.sleep(interval)
        reads.append(fetch_health(base, city_id))
    return realtime_signal(consolidate_realtime(reads), rt_floor, rt_max_age, rt_min_vehicles)


def drift_is_actionable(signals: list[str]) -> bool:
    """Given the realtime signal of every below-threshold city, is a rebuild owed today?

    A drop in overlap is the agency re-issuing trip ids; it only costs riders — and is only ours to
    fix today — once realtime has followed. 'moved' is that confirmation; 'unknown' fails loud so an
    unreachable API on a drifted city never goes silent. 'steady' and 'feeddown' are not ours today.
    """
    return any(s in ("moved", "unknown") for s in signals)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("city", nargs="?", help="city id, e.g. bogota")
    ap.add_argument("--all", action="store_true", help="every city whose file declares a baseline")
    ap.add_argument("--threshold", type=float, default=float(os.environ.get("DRIFT_THRESHOLD", DEFAULT_THRESHOLD)),
                    help=f"exit 1 when the overlap is under this percentage (default {DEFAULT_THRESHOLD})")
    ap.add_argument("--write-baseline", nargs=2, metavar=("GTFS_ZIP", "OUT_GZ"),
                    help="write the trip ids of a feed, for publishing beside graph.obj")
    # Two unrelated things can be wrong, and they want different answers: drift means rebuild a
    # graph today, a calendar running out means wait for the agency to republish. Collapsing both
    # into one exit code kept the daily check red for weeks on a calendar nobody could act on, and
    # a real threshold crossing then changed nothing visible. Measuring still always reports both;
    # this only chooses what counts as failure.
    ap.add_argument("--check", choices=("drift", "calendar", "both"), default="both",
                    help="which signal decides the exit code (default both)")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"API base for the realtime cross-check (default {DEFAULT_BASE})")
    ap.add_argument("--rt-floor", type=float, default=RT_FLOOR,
                    help="rtById below this, on a live feed, means realtime has moved — so a drifted "
                         f"graph is worth rebuilding today (default {RT_FLOOR:g})")
    ap.add_argument("--rt-max-age", type=float, default=RT_MAX_AGE,
                    help="median realtime entity age (s) above which a low rtById is read as a "
                         f"positions-feed outage, not drift (default {RT_MAX_AGE:g})")
    ap.add_argument("--rt-min-vehicles", type=int, default=RT_MIN_VEHICLES,
                    help=f"vehicle count below which a low rtById is a feed outage (default {RT_MIN_VEHICLES})")
    ap.add_argument("--samples", type=int, default=3,
                    help="/health reads used to confirm a 'moved' signal; rtById is judged by its "
                         "best read, so one noisy dip on a small fleet cannot redden the alarm")
    ap.add_argument("--sample-interval", type=float, default=20.0,
                    help="seconds between the confirmation samples (default 20)")
    a = ap.parse_args()

    if a.write_baseline:
        n = write_baseline(*a.write_baseline)
        print(f"{n:,} trip ids -> {a.write_baseline[1]}")
        return 0

    # `_template.yaml` carries placeholders rather than a city, so it is not one.
    cities = (sorted(p.stem for p in (ROOT / "cities").glob("*.yaml") if not p.name.startswith("_"))
              if a.all else ([a.city] if a.city else []))
    if not cities:
        ap.error("name a city or pass --all")

    want_drift = a.check in ("drift", "both")
    drift_signals: list[str] = []
    calendar_alarm = False
    for cid in cities:
        try:
            r = check(cid, a.threshold)
        except Exception as e:  # noqa: BLE001
            print(f"{cid:14} error: {type(e).__name__}: {e}")
            continue
        if r.get("skipped"):
            if not a.all:
                print(f"{cid:14} skipped: {r['skipped']}")
            continue

        flag = ""
        if r["rebuild"]:
            # The overlap says the feed moved; ask realtime whether that has reached riders yet —
            # but only pay the /health call when a drift verdict can actually use it.
            if want_drift:
                sig = confirmed_signal(a.base, cid, a.rt_floor, a.rt_max_age, a.rt_min_vehicles,
                                       a.samples, a.sample_interval)
                drift_signals.append(sig)
                flag += {"moved": "  REBUILD",
                         "steady": "  drift (realtime steady — hold)",
                         "feeddown": "  drift (positions feed down — not ours)",
                         "unknown": "  REBUILD? (realtime unreadable — check)"}[sig]
            else:
                flag += "  REBUILD"
        if r.get("calendarWarning"):
            flag += f"  CALENDAR ENDS IN {r['calendarDaysLeft']}d ({r['calendarEnds']})"
            calendar_alarm = True
        print(f"{cid:14} {r['overlapPct']:5.1f}%  graph {r['graphTrips']:>7,} · feed {r['feedTrips']:>7,}"
              f" · shared {r['sharedTrips']:>7,}{flag}")

    drift_alarm = drift_is_actionable(drift_signals)
    failing = {"drift": drift_alarm, "calendar": calendar_alarm,
               "both": drift_alarm or calendar_alarm}[a.check]
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
