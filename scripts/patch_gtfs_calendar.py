"""Extend an expired GTFS calendar so OTP keeps serving a feed whose publisher forgot to roll it.

    python scripts/patch_gtfs_calendar.py data/casablanca/casablanca-gtfs.zip 20270630

Casablanca's community feed (github.com/SpaghettDev/CasaTransport-GTFS) ended on 2026-06-30 while the
tramway kept running unchanged; run this before scripts/build-graph.sh until the publisher regenerates it.
Only calendar.txt end_date changes; calendar_dates.txt is kept as published (holiday switches).
"""
import csv
import io
import shutil
import sys
import zipfile


def main(path: str, end_date: str) -> None:
    tmp = path + ".patched"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == "calendar.txt":
                rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
                for r in rows:
                    r["end_date"] = max(r["end_date"], end_date)
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
                w.writeheader()
                w.writerows(rows)
                data = buf.getvalue().encode()
                print(f"calendar.txt: {len(rows)} services now end no earlier than {end_date}")
            zout.writestr(name, data)
    shutil.move(tmp, path)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
