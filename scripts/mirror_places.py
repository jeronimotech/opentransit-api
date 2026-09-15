#!/usr/bin/env python3
"""Download a city's named-area layers from its GIS server and publish them as plain GeoJSON files, for
an API that cannot reach that server itself (Catastro Bogotá's answers Colombian connections only).

    python scripts/mirror_places.py bogota            # writes build/places/bogota-{barrios,localidades}.geojson
    python scripts/mirror_places.py bogota --upload   # …and uploads them to the GitHub release `places-bogota`

The city's YAML then points `geocoder.areas.*_url` at the release assets; the API mirrors them monthly.
Run from a network that reaches the GIS server (a laptop in Bogotá does)."""
import argparse
import asyncio
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.places import fetch_arcgis_layer  # noqa: E402

# Catastro's reference map: sectores catastrales (barrios) and localidades
SOURCES = {
    "bogota": {
        "barrios": "https://serviciosgis.catastrobogota.gov.co/arcgis/rest/services/Mapa_Referencia/Mapa_Referencia/MapServer/37",
        "localidades": "https://serviciosgis.catastrobogota.gov.co/arcgis/rest/services/Mapa_Referencia/Mapa_Referencia/MapServer/48",
    },
}


def _round(coords, nd=6):
    if isinstance(coords, (int, float)):
        return round(coords, nd)
    return [_round(c, nd) for c in coords]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("city")
    ap.add_argument("--upload", action="store_true", help="upload to the GitHub release places-<city> (gh CLI)")
    ap.add_argument("--repo", default="jeronimotech/opentransit-api")
    args = ap.parse_args()
    srcs = SOURCES.get(args.city)
    if not srcs:
        print(f"no sources known for {args.city}; add them to SOURCES", file=sys.stderr)
        return 2
    out = pathlib.Path("build/places")
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for kind, url in srcs.items():
        feats = await fetch_arcgis_layer(url)
        for f in feats:
            if f.get("geometry"):
                f["geometry"]["coordinates"] = _round(f["geometry"]["coordinates"])
        path = out / f"{args.city}-{kind}.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
        print(f"{path}: {len(feats)} features, {path.stat().st_size / 1e6:.1f} MB")
        files.append(path)
    if args.upload:
        tag = f"places-{args.city}"
        if subprocess.run(["gh", "release", "view", tag, "-R", args.repo], capture_output=True).returncode != 0:
            subprocess.run(["gh", "release", "create", tag, "-R", args.repo, "--title", f"Named areas · {args.city}",
                            "--notes", "Neighbourhood and district polygons mirrored from the city's cadastre. "
                                       "Refreshed by scripts/mirror_places.py; the API reads these files."],
                           check=True)
        subprocess.run(["gh", "release", "upload", tag, *map(str, files), "-R", args.repo, "--clobber"], check=True)
        for f in files:
            print(f"https://github.com/{args.repo}/releases/download/{tag}/{f.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
