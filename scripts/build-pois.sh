#!/usr/bin/env bash
# Build cities/<slug>/pois.geojson from OpenStreetMap via the Overpass API (station services layer).
# Usage: scripts/build-pois.sh <city-slug> [overpass-url]
# Polite by design: one query, 180 s timeout, one retry, result cached in the repo (commit it).
set -euo pipefail
CITY="${1:?usage: build-pois.sh <city>}"
OVERPASS="${2:-${OVERPASS_URL:-https://overpass-api.de/api/interpreter}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
YAML="$ROOT/cities/$CITY.yaml"
OUT_DIR="$ROOT/cities/$CITY"
OUT="$OUT_DIR/pois.geojson"
PY="${PY:-$ROOT/.venv/bin/python}"; [ -x "$PY" ] || PY=python3
[ -f "$YAML" ] || { echo "no such city: $YAML" >&2; exit 1; }
mkdir -p "$OUT_DIR"

read -r W S E N < <("$PY" - "$YAML" <<'PYEOF'
import sys, yaml
c = yaml.safe_load(open(sys.argv[1]))
print(*c["bbox"])
PYEOF
)
BBOX="$S,$W,$N,$E"   # Overpass wants south,west,north,east
echo "city=$CITY bbox=$BBOX overpass=$OVERPASS"

QUERY=$(cat <<EOQ
[out:json][timeout:180];
(
  nwr["amenity"="bicycle_parking"]($BBOX);
  nwr["amenity"="toilets"]($BBOX);
  nwr["amenity"="atm"]($BBOX);
  nwr["amenity"~"^(hospital|clinic|doctors)$"]($BBOX);
  nwr["amenity"="library"]($BBOX);
  nwr["amenity"="police"]($BBOX);
  nwr["amenity"="pharmacy"]($BBOX);
);
out center tags;
EOQ
)
RAW="$(mktemp -t pois.XXXX).json"
for attempt in 1 2; do
  if curl -sS --max-time 200 -A "opentransit-api build-pois (https://github.com/jeronimotech/opentransit-api)" \
       --data-urlencode "data=$QUERY" "$OVERPASS" -o "$RAW" && "$PY" -c "import json,sys; json.load(open('$RAW'))" 2>/dev/null; then
    break
  fi
  echo "overpass attempt $attempt failed" >&2; [ "$attempt" = 2 ] && exit 2; sleep 30
done

"$PY" - "$RAW" "$OUT" "$CITY" <<'PYEOF'
import json, sys, datetime as dt
raw, out, city = sys.argv[1:4]
els = json.load(open(raw)).get("elements", [])
TYPE = {"bicycle_parking": "bike_parking", "toilets": "toilets", "atm": "atm", "hospital": "health",
        "clinic": "health", "doctors": "health", "library": "library", "police": "police", "pharmacy": "pharmacy"}
feats = []
for e in els:
    t = e.get("tags") or {}
    kind = TYPE.get(t.get("amenity"))
    if not kind:
        continue
    lat, lon = (e.get("lat"), e.get("lon")) if e["type"] == "node" else ((e.get("center") or {}).get("lat"), (e.get("center") or {}).get("lon"))
    if lat is None or lon is None:
        continue
    props = {"id": f"osm:{e['type'][0]}{e['id']}", "type": kind, "name": t.get("name") or t.get("operator") or None,
             "source": "osm", "osmId": f"{e['type']}/{e['id']}", "wheelchair": t.get("wheelchair"),
             "operator": t.get("operator"), "openingHours": t.get("opening_hours"),
             "capacity": t.get("capacity"), "fee": t.get("fee"), "covered": t.get("covered")}
    feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                  "properties": {k: v for k, v in props.items() if v is not None}})
by = {}
for f in feats:
    by[f["properties"]["type"]] = by.get(f["properties"]["type"], 0) + 1
doc = {"type": "FeatureCollection", "meta": {"city": city, "source": "OpenStreetMap contributors (ODbL) via Overpass",
       "generatedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"), "counts": by},
       "features": feats}
json.dump(doc, open(out, "w"), ensure_ascii=False, separators=(",", ":"))
print(f"wrote {out}: {len(feats)} features {by}")
PYEOF
