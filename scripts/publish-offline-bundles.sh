#!/usr/bin/env bash
# Build, publish and wire up the offline timetable bundles.
#
#   scripts/publish-offline-bundles.sh                 # all nine cities
#   scripts/publish-offline-bundles.sh bogota roma     # just these
#   DRY_RUN=1 scripts/publish-offline-bundles.sh       # build and report, upload nothing
#
# One release holds every city's bundle, rather than one release per city like the graphs: a bundle
# is small, they are rebuilt together, and a rider's app asks for its own city's asset by name.
#
# Afterwards each cities/<city>.yaml gets an `offline:` block pointing at its asset, with the byte
# size the app shows before a rider commits to the download. The YAML edit is the half people
# forget: without it the asset exists and no app will ever ask for it.
#
# Needs: `gh` logged in to the org, and data/<city>/<city>-gtfs.zip (a graph build leaves it there).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ALL=(bogota boston brisbane casablanca kualalumpur lisboa roma santiago toronto)
CITIES=("${@:-}")
[ -z "${CITIES[0]:-}" ] && CITIES=("${ALL[@]}")

TAG="offline-bundles-$(date +%F)"
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo jeronimotech/opentransit-api)"
OUT="$ROOT/data/_bundles"
mkdir -p "$OUT"

echo "repo:  $REPO"
echo "tag:   $TAG"
echo

built=()
for city in "${CITIES[@]}"; do
  zip="data/$city/$city-gtfs.zip"
  if [ ! -s "$zip" ]; then
    echo "SKIP $city: no $zip (run scripts/build-graph.sh $city first)" >&2
    continue
  fi
  # The builder refuses to write a bundle in which no service runs today, so a feed whose calendar
  # has run out fails here rather than shipping a timetable that shows nothing.
  python3 scripts/build_offline_bundle.py "$city" --out "$OUT/offline-$city.ndjson.gz"
  built+=("$city")
done

[ ${#built[@]} -eq 0 ] && { echo "nothing built" >&2; exit 1; }

echo
if [ -n "${DRY_RUN:-}" ]; then
  echo "DRY_RUN: would upload to $TAG"
  for city in "${built[@]}"; do
    f="$OUT/offline-$city.ndjson.gz"
    printf "  %-13s %6.2f MB  ->  offline-%s.ndjson.gz\n" "$city" "$(echo "scale=4; $(wc -c < "$f")/1048576" | bc)" "$city"
  done
  exit 0
fi

assets=()
for city in "${built[@]}"; do assets+=("$OUT/offline-$city.ndjson.gz"); done

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  gh release upload "$TAG" "${assets[@]}" --repo "$REPO" --clobber
else
  gh release create "$TAG" "${assets[@]}" --repo "$REPO" \
    --title "Offline timetables $(date +%F)" \
    --notes "Downloadable GTFS timetables, one NDJSON bundle per city. Built by scripts/build_offline_bundle.py."
fi

echo
echo "pointing the city YAMLs at the new assets…"
for city in "${built[@]}"; do
  f="$OUT/offline-$city.ndjson.gz"
  bytes=$(wc -c < "$f" | tr -d ' ')
  url="https://github.com/$REPO/releases/download/$TAG/offline-$city.ndjson.gz"
  python3 - "$city" "$url" "$bytes" <<'PY'
import datetime as dt, pathlib, re, sys
city, url, nbytes = sys.argv[1], sys.argv[2], int(sys.argv[3])
p = pathlib.Path("cities") / f"{city}.yaml"
s = p.read_text()
block = (f"offline:\n"
         f"  url: {url}\n"
         f"  bytes: {nbytes}\n"
         f"  built_at: \"{dt.date.today()}\"\n")
if re.search(r"^offline:\n(?:[ \t].*\n|\n)*", s, re.M):
    s = re.sub(r"^offline:\n(?:[ \t].*\n|\n)*", block, s, count=1, flags=re.M)
else:
    s = s.rstrip() + "\n\n# v1.6 offline timetables. Written by scripts/publish-offline-bundles.sh.\n" + block
p.write_text(s)
print(f"  {city:13} {nbytes/1048576:5.2f} MB")
PY
done

cat <<'NEXT'

Done. Two things left, and the API will not serve the new field without them:

  git add cities/*.yaml && git commit -m "offline: point the cities at the published bundles"
  git push
  RAILWAY_TOKEN="$RAILWAY_TOKEN_PROD" railway up -s api -e production -d -c

Then check one:  curl -s https://api.opentransit.tech/v1/cities/bogota | python3 -m json.tool | grep -A5 offline
NEXT
