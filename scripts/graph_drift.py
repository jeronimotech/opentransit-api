#!/usr/bin/env python3
"""Measure, and optionally gate on, how far a city's deployed OTP graph has drifted from its feed.

    scripts/graph_drift.py bogota                 # print the overlap
    scripts/graph_drift.py --all                  # every city that publishes a baseline
    scripts/graph_drift.py bogota --threshold 85  # exit 1 when it has fallen below

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
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UA = {"User-Agent": "opentransit-graph-drift/1.0 (+https://opentransit.tech)"}
DEFAULT_THRESHOLD = 85.0
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("city", nargs="?", help="city id, e.g. bogota")
    ap.add_argument("--all", action="store_true", help="every city whose file declares a baseline")
    ap.add_argument("--threshold", type=float, default=float(os.environ.get("DRIFT_THRESHOLD", DEFAULT_THRESHOLD)),
                    help=f"exit 1 when the overlap is under this percentage (default {DEFAULT_THRESHOLD})")
    ap.add_argument("--write-baseline", nargs=2, metavar=("GTFS_ZIP", "OUT_GZ"),
                    help="write the trip ids of a feed, for publishing beside graph.obj")
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

    worst = 100.0
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
        flag = "  REBUILD" if r["rebuild"] else ""
        if r.get("calendarWarning"):
            flag += f"  CALENDAR ENDS IN {r['calendarDaysLeft']}d ({r['calendarEnds']})"
        print(f"{cid:14} {r['overlapPct']:5.1f}%  graph {r['graphTrips']:>7,} · feed {r['feedTrips']:>7,}"
              f" · shared {r['sharedTrips']:>7,}{flag}")
        worst = min(worst, r["overlapPct"])
        if r.get("calendarWarning"):
            calendar_alarm = True
    return 1 if (worst < a.threshold or calendar_alarm) else 0


if __name__ == "__main__":
    sys.exit(main())
