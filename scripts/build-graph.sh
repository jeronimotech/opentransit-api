#!/usr/bin/env bash
# Build the OpenTripPlanner graph for one city.
#   scripts/build-graph.sh <city> [--force-download]
# Reads otp/<city>/sources.env (GTFS_URL, OSM_URL, BBOX, BUILD_HEAP), downloads the GTFS zip and the
# regional OSM extract, clips OSM to BBOX with osmium (Docker), then runs `otp --build --save`.
# Output: data/<city>/graph.obj (+ the inputs), mounted read-only by the otp-<city> compose service.
set -euo pipefail
CITY="${1:?usage: build-graph.sh <city>}"
FORCE="${2:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OTP_IMAGE="${OTP_IMAGE:-opentripplanner/opentripplanner:2.9.0}"
# OTP_RUNTIME=docker|native. Docker needs a VM with >= 12 GB for Bogotá; on macOS use native (see otp-native.sh).
OTP_RUNTIME="${OTP_RUNTIME:-$( [ "$(uname)" = Darwin ] && echo native || echo docker )}"
OSMIUM_IMAGE="${OSMIUM_IMAGE:-stefda/osmium-tool:latest}"   # entrypoint is a shell, so the command starts with `osmium`
CFG="$ROOT/otp/$CITY"
DATA="$ROOT/data/$CITY"
[ -f "$CFG/sources.env" ] || { echo "missing $CFG/sources.env" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CFG/sources.env"
mkdir -p "$DATA"
cd "$DATA"

REGION_PBF="$(basename "$OSM_URL")"
fresh() { [ -z "$FORCE" ] && [ -s "$1" ] && [ -z "$(find "$1" -mtime +1 2>/dev/null)" ]; }

if fresh "$CITY-gtfs.zip"; then echo "[gtfs] using cached $CITY-gtfs.zip"; else
  echo "[gtfs] downloading $GTFS_URL"; curl -fsSL --retry 3 -o "$CITY-gtfs.zip" "$GTFS_URL"; fi
if fresh "$REGION_PBF"; then echo "[osm] using cached $REGION_PBF"; else
  echo "[osm] downloading $OSM_URL"; curl -fsSL --retry 3 -o "$REGION_PBF" "$OSM_URL"; fi

echo "[osm] clipping $REGION_PBF to $BBOX -> $CITY.osm.pbf"
docker run --rm -v "$DATA:/data" "$OSMIUM_IMAGE" osmium \
  extract --overwrite -s complete_ways -b "$BBOX" "/data/$REGION_PBF" -o "/data/$CITY.osm.pbf"
ls -lh "$CITY.osm.pbf"

if [ "$OTP_RUNTIME" = native ]; then
  exec "$ROOT/scripts/otp-native.sh" build "$CITY"
fi
cp "$CFG/build-config.json" "$CFG/router-config.json" "$DATA/"
echo "[otp] building graph in Docker with heap ${BUILD_HEAP:-8G} (this takes 10-40 min for a big feed)"
t0=$(date +%s)
docker run --rm -e "JAVA_OPTS=-Xmx${BUILD_HEAP:-8G}" -v "$DATA:/var/opentripplanner" \
  "$OTP_IMAGE" --build --save   # the image entrypoint appends /var/opentripplanner itself
echo "[otp] graph built in $(( $(date +%s) - t0 )) s -> $(ls -lh graph.obj | awk '{print $5}')"
