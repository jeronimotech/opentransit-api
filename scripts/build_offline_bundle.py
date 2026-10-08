#!/usr/bin/env python3
"""Build one city's offline timetable bundle from its GTFS zip.

    scripts/build_offline_bundle.py <city> [--data data] [--out -] [--stats]

Published as a release asset next to the graph, the way `trip-ids.txt.gz` already is, and pointed at
from the city YAML. The API cannot serve this from its own database: `stop_times.txt` is streamed
once at ingest and never stored (see gtfs_static.py), so departures come from OTP, per stop, over
the network — exactly what is unavailable to a rider underground.

**Why a whole city fits.** Measured on the real feeds, 2026-10-07: Roma's five million departures
come to 2.1 MB gzipped, Toronto's 4.3 million to 0.7 MB. Two ideas do that work:

- A departure board needs far less than a trip. It wants, at each stop, a time and which service
  it belongs to — not the trip's whole stop sequence, shape or block.
- Departures on one route at one stop are a sorted list a few minutes apart, so storing the first
  and then the gaps turns five-digit numbers into one-digit ones, which gzip then eats.

A compression that looked obvious and was measured away: grouping trips by pattern and storing the
running-time offsets once. Only 22-41 % of real trips keep their pattern's offsets, because dwell
and running times vary by hour, so the deviations cost more than the sharing saved. Ratio 1.0x.

So the slice is: everything. Every service the feed describes, for as long as it describes it. No
expiry window to get wrong, no partial-coverage edge cases, and the whole thing is a smaller
download than one screenshot.

**Why it is NDJSON and not one object.** Small on the wire is not the same as small in memory.
Expanded into client objects, Lisboa's twelve million departures are about 160 MB resident and
several hundred at the peak of a whole-document parse, which gets an app killed on a mid-range
phone. So the file is one header line — stops, routes, headsigns, calendar, around a megabyte, and
the only part a client keeps — followed by one line per stop. A client records each line's offset
once at install and afterwards reads exactly the stop a rider is looking at.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

FORMAT_VERSION = 1


def _rows(z: zipfile.ZipFile, name: str):
    """Stream one GTFS table. `stop_times.txt` is 500 MB for Bogota, so nothing is read whole."""
    if name not in z.namelist():
        return
    with z.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig"))


def _secs(s: str | None) -> int | None:
    """GTFS `H:MM:SS`, where hours may pass 24 for a trip that runs past midnight."""
    if not s:
        return None
    try:
        h, m, sec = s.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec)
    except ValueError:
        return None


class Interner:
    """A string table, so a headsign repeated by ten thousand trips is stored once."""

    def __init__(self) -> None:
        self.values: list[str] = []
        self._index: dict[str, int] = {}

    def __call__(self, s: str) -> int:
        i = self._index.get(s)
        if i is None:
            i = len(self.values)
            self._index[s] = i
            self.values.append(s)
        return i

    def get(self, s: str) -> int | None:
        return self._index.get(s)


def build_patterns(zip_path: Path, city: str) -> dict:
    """The same timetable, indexed by pattern instead of by stop — what journey planning needs.

    A departure board answers "when does something leave here", and the shipped bundle is shaped for
    exactly that: per stop, times grouped by route. It cannot answer "if I board at A, when do I
    reach B", because nothing links a departure at one stop to an arrival at another.

    A pattern — one ordered stop sequence, with every trip that runs it — restores that link, and is
    what RAPTOR and connection-scan both consume. Measured on Bogota: 1 521 patterns over 181 051
    trips and 9 471 772 times, 8.06 MB gzipped against the board bundle's 5.42 MB. A 49 % larger
    download that serves both questions, since a board is derivable from the patterns calling at a
    stop.

    Emitted as its own artefact rather than folded into the bundle, so the shipped format and the
    client reading it stay exactly as they are until there is something on the other side to use
    this. Nothing downloads it yet.

    **Memory**: this holds every trip's stop sequence at once, because `stop_times.txt` is not
    sorted by trip in every feed — Bogota's is not, and streaming it as though it were split each
    trip into fragments and reported 1.35 million patterns instead of 1 521.
    """
    z = zipfile.ZipFile(zip_path)
    if "stop_times.txt" not in z.namelist():
        raise NotATimetable(f"{zip_path} has no stop_times.txt")

    trips: dict[str, tuple[str, str, str]] = {}
    for r in _rows(z, "trips.txt"):
        trips[r["trip_id"]] = (r.get("route_id") or "", r.get("service_id") or "",
                               (r.get("trip_headsign") or "").strip())

    stops_ix = Interner()
    for r in _rows(z, "stops.txt"):
        stops_ix(r["stop_id"])

    # The same frequency expansion the board builder does, and for the same reason: four of nine
    # feeds use frequencies.txt, Casablanca's every trip is one, and reading stop_times literally
    # gave it 36 trips instead of thousands — a planner that finds nothing in the city with no
    # realtime at all, which is the city that needs offline most.
    freqs: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for r in _rows(z, "frequencies.txt"):
        start, end = _secs(r.get("start_time")), _secs(r.get("end_time"))
        headway = int(r.get("headway_secs") or 0)
        if start is None or end is None or headway <= 0 or end < start:
            continue
        freqs[r["trip_id"]].append((start, end, headway))

    seqs: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for r in _rows(z, "stop_times.txt"):
        tid = r["trip_id"]
        if tid not in trips:
            continue
        t = _secs(r.get("departure_time") or r.get("arrival_time"))
        si = stops_ix.get(r.get("stop_id") or "")
        if t is None or si is None:
            continue
        seqs[tid].append((int(r.get("stop_sequence") or 0), si, t))

    routes_ix, heads_ix, svc_ix = Interner(), Interner(), Interner()
    patterns: dict[tuple, int] = {}
    pattern_stops: list[list[int]] = []
    pattern_meta: list[tuple[int, int]] = []
    runs: list[list] = []
    for tid, seq in seqs.items():
        if len(seq) < 2:
            continue
        seq.sort()
        route, service, headsign = trips[tid]
        key = (route, headsign, tuple(s for _, s, _ in seq))
        i = patterns.get(key)
        if i is None:
            i = patterns[key] = len(pattern_stops)
            pattern_stops.append([s for _, s, _ in seq])
            pattern_meta.append((routes_ix(route), heads_ix(headsign)))
            runs.append([])
        svc = svc_ix(service)
        base = seq[0][2]
        offsets = [t - base for _, _, t in seq]
        windows = freqs.get(tid)
        if not windows:
            runs[i].append([svc, [t // 60 for _, _, t in seq]])
            continue
        for start, end, headway in windows:
            for depart in range(start, end, headway):
                runs[i].append([svc, [(depart + o) // 60 for o in offsets]])

    for r in runs:
        r.sort(key=lambda x: x[1][0] if x[1] else 0)

    return {
        "v": 1,
        "city": city,
        "builtAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stops": stops_ix.values,
        "routes": routes_ix.values,
        "headsigns": heads_ix.values,
        "services": svc_ix.values,
        # One entry per pattern: the stops it calls at, which route and headsign it is, and every
        # trip that runs it as [service, times], sorted by departure so a scan can binary-search.
        "patterns": [
            {"r": pattern_meta[i][0], "h": pattern_meta[i][1], "s": pattern_stops[i], "t": runs[i]}
            for i in range(len(pattern_stops))
        ],
        "stats": {
            "patterns": len(pattern_stops),
            "trips": sum(len(r) for r in runs),
            "times": sum(len(t[1]) for r in runs for t in r),
        },
    }


def _city_config(city: str):
    """The city's own YAML, for the agency-to-component mapping the app draws with.

    Tolerated when absent, because this script is useful against a bare GTFS zip and a missing
    config should cost the component rather than the whole bundle — but it says so. The first
    version swallowed the failure silently and shipped a bundle whose every route was uncoloured,
    which looked exactly like a bundle that had no component mapping to apply.

    `sys.path` needs the repo root: running `python3 scripts/build_offline_bundle.py` puts
    `scripts/` on the path, not the package above it.
    """
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    path = root / "cities" / f"{city}.yaml"
    if not path.exists():
        print(f"{city}: no cities/{city}.yaml — routes will carry no component", file=sys.stderr)
        return None
    try:
        from app.cities import load_city_file
        return load_city_file(path)
    except Exception as e:                                        # noqa: BLE001 - reported, not hidden
        print(f"{city}: could not read cities/{city}.yaml ({type(e).__name__}: {e}); "
              f"routes will carry no component", file=sys.stderr)
        return None


class NotATimetable(ValueError):
    """The zip cannot produce a timetable, so there is nothing honest to publish."""


def build(zip_path: Path, city: str) -> dict:
    z = zipfile.ZipFile(zip_path)
    # Without stop_times there is no timetable, and every table below would quietly yield nothing:
    # the result is a structurally valid bundle with an empty board for every stop, which is the
    # worst outcome available — it installs, it validates, and it tells a rider their bus never runs.
    if "stop_times.txt" not in z.namelist():
        raise NotATimetable(f"{zip_path} has no stop_times.txt")

    # The app colours and ices routes by *component* — trunk, dual, zonal, cable — not by the
    # GTFS colour, and that mapping lives in the city's own config. Without it every offline route
    # chip fell back to a generic grey, so a downloaded board looked like a different app from the
    # one online. Seen on a real phone.
    cfg = _city_config(city)

    routes_ix = Interner()
    routes: list[dict] = []
    for r in _rows(z, "routes.txt"):
        routes_ix(r["route_id"])
        colour = (r.get("route_color") or "").strip()
        text = (r.get("route_text_color") or "").strip()
        routes.append({
            "id": r["route_id"],
            "short": (r.get("route_short_name") or "").strip() or None,
            "long": (r.get("route_long_name") or "").strip() or None,
            "color": f"#{colour}" if colour else None,
            "text": f"#{text}" if text else None,
            "type": int(r.get("route_type") or 3),
            "component": cfg.component_of_route(r.get("agency_id"), r.get("route_type"))
            if cfg else None,
        })

    stops_ix = Interner()
    stops: list[dict] = []
    for r in _rows(z, "stops.txt"):
        stops_ix(r["stop_id"])
        try:
            lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
        except (KeyError, TypeError, ValueError):
            lat = lon = 0.0
        stops.append({
            "id": r["stop_id"],
            "name": (r.get("stop_name") or "").strip(),
            # Five decimals is about a metre. A feed publishing seven is publishing noise, and the
            # extra digits cost more than the whole headsign table.
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "code": (r.get("stop_code") or "").strip() or None,
            "type": int(r.get("location_type") or 0),
            "parent": (r.get("parent_station") or "").strip() or None,
        })

    heads_ix = Interner()
    svc_ix = Interner()
    # trip_id -> (route index, headsign index, service index)
    trip_meta: dict[str, tuple[int, int, int]] = {}
    for r in _rows(z, "trips.txt"):
        trip_meta[r["trip_id"]] = (
            routes_ix(r.get("route_id") or ""),
            heads_ix((r.get("trip_headsign") or "").strip()),
            svc_ix(r.get("service_id") or ""),
        )

    # A frequency-based trip appears once in stop_times and actually runs every `headway_secs`
    # between start and end. Four of our nine feeds use it, and for Santiago that is 56 % of trips
    # and for Casablanca all of them: reading stop_times alone would publish a timetable with one
    # departure a day and call it the schedule.
    freqs: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for r in _rows(z, "frequencies.txt"):
        start, end = _secs(r.get("start_time")), _secs(r.get("end_time"))
        headway = int(r.get("headway_secs") or 0)
        if start is None or end is None or headway <= 0 or end < start:
            continue
        freqs[r["trip_id"]].append((start, end, headway))

    # A frequency trip's stop_times are a template measured from the trip's own first departure, so
    # expanding it needs that first departure. Streaming forbids looking a row up, so take one extra
    # pass over stop_times reading only these trips. Skipped entirely for the five feeds with no
    # frequencies.txt, and cheap for the four that have one: Santiago's 14 520 frequency trips are
    # the largest case.
    first_departure: dict[str, int] = {}
    if freqs:
        for r in _rows(z, "stop_times.txt"):
            tid = r["trip_id"]
            if tid not in freqs:
                continue
            t = _secs(r.get("departure_time") or r.get("arrival_time"))
            if t is None:
                continue
            prev = first_departure.get(tid)
            if prev is None or t < prev:
                first_departure[tid] = t

    # (stop, route, headsign, service) -> departure minutes
    groups: dict[tuple[int, int, int, int], list[int]] = defaultdict(list)
    skipped_rows = 0
    for r in _rows(z, "stop_times.txt"):
        meta = trip_meta.get(r["trip_id"])
        if meta is None:
            skipped_rows += 1
            continue
        t = _secs(r.get("departure_time") or r.get("arrival_time"))
        si = stops_ix.get(r.get("stop_id") or "")
        if t is None or si is None:
            skipped_rows += 1
            continue
        key = (si, meta[0], meta[1], meta[2])
        windows = freqs.get(r["trip_id"])
        if not windows:
            groups[key].append(t // 60)
            continue
        base = first_departure.get(r["trip_id"])
        if base is None:
            skipped_rows += 1
            continue
        offset = t - base
        for start, end, headway in windows:
            for depart in range(start, end, headway):
                groups[key].append((depart + offset) // 60)

    boards: dict[str, list] = {}
    total_departures = 0
    for (si, ri, hi, sv), times in groups.items():
        times.sort()
        total_departures += len(times)
        deltas = [times[0]]
        for i in range(1, len(times)):
            deltas.append(times[i] - times[i - 1])
        boards.setdefault(str(si), []).append([ri, hi, sv, deltas])

    # Every service a trip refers to, whether or not calendar.txt mentions it. Roma and Lisboa ship
    # no calendar.txt at all and define service purely through calendar_dates.txt, so iterating
    # calendar.txt alone emitted an empty service list for them — and a bundle whose services are
    # empty is a bundle where nothing ever runs: 2.1 MB and 4.3 MB of departures that would never
    # appear on a board. Two of nine cities, silently dead.
    cal: dict[str, dict] = {}
    for r in _rows(z, "calendar.txt"):
        cal[r["service_id"]] = {
            "days": [int(r.get(d) or 0) for d in
                     ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")],
            "from": (r.get("start_date") or "").strip(),
            "to": (r.get("end_date") or "").strip(),
        }
    services = []
    for sid in svc_ix.values:
        c = cal.get(sid)
        services.append({
            "id": sid,
            "idx": svc_ix.get(sid),
            # No calendar.txt row means the service runs only on the dates calendar_dates.txt adds,
            # which is a complete and valid GTFS calendar, not a gap.
            "days": c["days"] if c else [0, 0, 0, 0, 0, 0, 0],
            "from": c["from"] if c else None,
            "to": c["to"] if c else None,
        })
    exceptions = []
    for r in _rows(z, "calendar_dates.txt"):
        i = svc_ix.get(r["service_id"])
        if i is None:
            continue
        exceptions.append([i, (r.get("date") or "").strip(), int(r.get("exception_type") or 1)])

    feed_info = next(iter(_rows(z, "feed_info.txt")), {})

    header = {
        "v": FORMAT_VERSION,
        "city": city,
        "builtAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "feedVersion": (feed_info.get("feed_version") or "").strip() or None,
        # Service ids are referenced by index from `boards`, so the order of this list is part of
        # the format. Same for routes, headsigns and stops.
        "routes": routes,
        "headsigns": heads_ix.values,
        "services": services,
        "serviceExceptions": exceptions,
        "stops": stops,
        "stats": {
            "departures": total_departures,
            "groups": len(groups),
            "skippedStopTimeRows": skipped_rows,
            "frequencyTrips": len(freqs),
            "servicesWithoutCalendar": sum(1 for s in services if s["from"] is None),
            # The one number that says the bundle is alive. A build that would ship a timetable
            # where nothing runs today is a build that should fail, not one to upload and discover.
            "activeServicesToday": len(active_services(services, exceptions, dt.date.today())),
            "stopsWithDepartures": len(boards),
        },
    }
    return {"header": header, "boards": boards}


def active_services(services: list[dict], exceptions: list[list], on: dt.date) -> set[int]:
    """Which service indices run on a date — the same rule the client has to apply.

    Kept here so the builder can refuse to publish a bundle that is dead on arrival, and so the
    rule is written down once in a place the client's implementation can be checked against.
    """
    ymd = on.strftime("%Y%m%d")
    exc = {(e[0], e[1]): e[2] for e in exceptions}
    out: set[int] = set()
    for s in services:
        i = s["idx"]
        kind = exc.get((i, ymd))
        if kind == 2:                      # explicitly removed for this date
            continue
        if kind == 1:                      # explicitly added for this date
            out.add(i)
            continue
        if s["from"] and s["to"] and s["from"] <= ymd <= s["to"] and s["days"][on.weekday()]:
            out.add(i)
    return out


def serialise(doc: dict) -> bytes:
    """One header line, then one line per stop.

    Newline-delimited so a client can index the file by offset in a single pass at install and then
    read one stop at a time.

    Stop lines stay in the order stop_times produced them, which is not sorted and is deliberate:
    that order puts consecutive stops of the same route next to each other, and consecutive stops of
    one route share almost identical delta patterns. Sorting by stop index scatters them beyond
    gzip's 32 KB window and doubled Toronto's download, 0.82 MB to 1.63 MB, for the same bytes
    uncompressed. The order is still deterministic — the file is streamed once and dict insertion
    order is stable — so two builds of an unchanged feed are byte-identical either way.
    """
    out = bytearray()
    out += json.dumps(doc["header"], separators=(",", ":"), ensure_ascii=False).encode()
    out += b"\n"
    for si in doc["boards"]:
        out += json.dumps({"s": int(si), "g": doc["boards"][si]},
                          separators=(",", ":"), ensure_ascii=False).encode()
        out += b"\n"
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("city")
    ap.add_argument("--data", default="data", help="directory holding <city>/<city>-gtfs.zip")
    ap.add_argument("--out", default=None,
                    help="output path, or - for stdout; default <data>/<city>/offline-bundle.json.gz")
    ap.add_argument("--stats", action="store_true", help="print sizes to stderr and write nothing")
    ap.add_argument("--patterns-out", default=None,
                    help="also write the pattern-indexed timetable here (journey planning); "
                         "nothing downloads it yet")
    a = ap.parse_args()

    zip_path = Path(a.data) / a.city / f"{a.city}-gtfs.zip"
    if not zip_path.exists():
        print(f"missing {zip_path}", file=sys.stderr)
        return 1

    doc = build(zip_path, a.city)
    head = doc["header"]
    raw = serialise(doc)
    gz = gzip.compress(raw, 9)
    st = head["stats"]
    header_bytes = raw.index(b"\n") + 1
    print(f"{a.city:13} departures={st['departures']:>9} groups={st['groups']:>7} "
          f"stops={len(head['stops']):>6} routes={len(head['routes']):>5} "
          f"services={len(head['services']):>4} activeToday={st['activeServicesToday']:>4} "
          f"header={header_bytes/1048576:>5.2f}M ndjson={len(raw)/1048576:>6.1f}M "
          f"gz={len(gz)/1048576:>5.2f}M", file=sys.stderr)
    if st["activeServicesToday"] == 0:
        print(f"{a.city}: no service runs today in this bundle — refusing to publish a dead "
              f"timetable. Check calendar.txt / calendar_dates.txt coverage.", file=sys.stderr)
        return 2
    if a.stats:
        return 0

    if a.patterns_out:
        pat = build_patterns(zip_path, a.city)
        pat_raw = json.dumps(pat, separators=(",", ":"), ensure_ascii=False).encode()
        pat_gz = gzip.compress(pat_raw, 9)
        Path(a.patterns_out).write_bytes(pat_gz)
        ps = pat["stats"]
        print(f"{a.city:13} patterns={ps['patterns']:>6} trips={ps['trips']:>7} "
              f"times={ps['times']:>9} gz={len(pat_gz)/1048576:>5.2f}M -> {a.patterns_out}",
              file=sys.stderr)

    out = Path(a.out) if a.out and a.out != "-" else (zip_path.parent / "offline-bundle.ndjson.gz")
    if a.out == "-":
        sys.stdout.buffer.write(gz)
    else:
        out.write_bytes(gz)
        print(f"wrote {out} ({len(gz)/1048576:.2f} MB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
