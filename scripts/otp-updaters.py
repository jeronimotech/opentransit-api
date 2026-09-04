#!/usr/bin/env python3
"""
Generate the OpenTripPlanner `vehicle-rental` updaters for a city from its YAML config.

    scripts/otp-updaters.py <city> [--check]

Reads `cities/<city>.yaml` → `mobility.bike_share[]` and rewrites the `vehicle-rental` entries of
`otp/<city>/router-config.json` (one updater per network, `network` = the YAML `network` id, url = `gbfs_url`).
All other updaters (GTFS-RT etc.) are left untouched. Idempotent; `--check` exits 1 when the file is out of date.
Adding a bike-share provider is therefore config only: edit the YAML, run this, restart OTP.
"""
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.cities import expand_env  # noqa: E402


def updaters_for(city: dict) -> list[dict]:
    nets = ((city.get("mobility") or {}).get("bike_share")) or []
    return [{
        "type": "vehicle-rental", "sourceType": "gbfs", "network": n["network"], "url": n["gbfs_url"],
        "language": (city.get("locale") or "en").split("-")[0], "frequency": "60s",
        "allowKeepingRentedVehicleAtDestination": False, "geofencingZones": True,
    } for n in nets]


def merge(router_config: dict, rental: list[dict]) -> dict:
    keep = [u for u in router_config.get("updaters", []) if u.get("type") != "vehicle-rental"]
    router_config["updaters"] = keep + rental
    return router_config


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    slug, check = sys.argv[1], "--check" in sys.argv
    city = yaml.safe_load(expand_env((ROOT / "cities" / f"{slug}.yaml").read_text(encoding="utf-8")))
    path = ROOT / "otp" / slug / "router-config.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    merged = merge(json.loads(json.dumps(current)), updaters_for(city))
    text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    if check:
        same = json.dumps(current, sort_keys=True) == json.dumps(merged, sort_keys=True)
        print(f"[otp-updaters] {path.relative_to(ROOT)} is {'up to date' if same else 'OUT OF DATE'}")
        return 0 if same else 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    nets = [u["network"] for u in merged["updaters"] if u["type"] == "vehicle-rental"]
    print(f"[otp-updaters] {path.relative_to(ROOT)}: {len(nets)} vehicle-rental updater(s) {nets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
