#!/usr/bin/env python3
"""Drop the trips a GTFS feed describes but does not define, so OTP can build the graph.

    scripts/clean_gtfs.py data/lisboa/lisboa-gtfs.zip

OTP refuses to build a feed whose `stop_times` reference a stop that is not in `stops.txt`:

    Trip <Trip BNA17_…> contains stop_time with no stop, location or group.

Carris Metropolitana's feed had 1,364 such rows out of 12.5 million on 2026-09-30, across 592 of its
386,486 trips. That is a publisher bug and it will come back, so this removes those trips rather than
waiting for a good day to rebuild.

Whole trips go, not the individual rows: a trip missing one of its stops still plans, and would quietly
tell riders it does not call there. 592 absent trips are honest; one wrong itinerary is not.

Rewrites the zip in place (keeping a `.orig` copy the first time) and streams `stop_times.txt`, which
is 90 % of these feeds and will not fit in memory.
"""
from __future__ import annotations

import argparse
import csv
import io
import shutil
import sys
import zipfile
from pathlib import Path

# Files keyed by trip_id that must lose the dropped trips too.
BY_TRIP = ("stop_times.txt", "frequencies.txt", "trips.txt", "attributions.txt")


def _reader(z: zipfile.ZipFile, name: str):
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig", newline=""))


def bad_trips(z: zipfile.ZipFile) -> tuple[set[str], int]:
    """Trips with a stop_time pointing at a stop the feed never defines, and how many such rows."""
    stops = {r["stop_id"] for r in _reader(z, "stops.txt") if r.get("stop_id")}
    bad: set[str] = set()
    rows = 0
    for r in _reader(z, "stop_times.txt"):
        sid = (r.get("stop_id") or "").strip()
        # `location_id` / `location_group_id` are the flex alternatives to a stop; a row with none of
        # the three is the case OTP rejects.
        if sid in stops or (r.get("location_id") or r.get("location_group_id") or "").strip():
            continue
        bad.add(r["trip_id"])
        rows += 1
    return bad, rows


def clean(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        drop, rows = bad_trips(z)
        if not drop:
            return {"droppedTrips": 0, "orphanRows": 0}
        total_trips = sum(1 for _ in _reader(z, "trips.txt"))
        orig = path.with_suffix(path.suffix + ".orig")
        if not orig.exists():
            shutil.copy2(path, orig)
        out = path.with_suffix(path.suffix + ".clean")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
            for item in z.infolist():
                name = item.filename
                if name.rsplit("/", 1)[-1] not in BY_TRIP:
                    w.writestr(item, z.read(name))
                    continue
                src = _reader(z, name)
                fields = src.fieldnames or []
                if "trip_id" not in fields:
                    w.writestr(item, z.read(name))
                    continue
                # Streamed straight into the archive: stop_times.txt is millions of rows and
                # building it in memory first costs more than the whole feed.
                with w.open(name, "w") as raw, io.TextIOWrapper(raw, encoding="utf-8", newline="") as dst:
                    out_csv = csv.DictWriter(dst, fieldnames=fields, lineterminator="\n")
                    out_csv.writeheader()
                    for r in src:
                        if r.get("trip_id") not in drop:
                            out_csv.writerow(r)
    out.replace(path)
    return {"droppedTrips": len(drop), "orphanRows": rows, "totalTrips": total_trips}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip", type=Path)
    a = ap.parse_args()
    r = clean(a.zip)
    if not r["droppedTrips"]:
        print("nothing to clean: every stop_time resolves to a stop")
        return 0
    print(f"dropped {r['droppedTrips']:,} of {r['totalTrips']:,} trips "
          f"({r['orphanRows']:,} stop_times pointed at stops the feed does not define)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
