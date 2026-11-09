#!/usr/bin/env bash
# Rebuild one city's OTP graph, publish it, point the services at it, redeploy — in one command.
#
# Collapses docs/DEPLOY-RAILWAY.md §1 + §6 (build → release → set GRAPH_URL/SHA on otp → bump
# trip_ids_url → redeploy otp + api) into a repeatable step so the remediation is no longer seven
# manual stages. Intended to run behind scripts/should-rebuild.py, on the self-hosted macOS runner
# (the only host with the 14 GB heap a Bogotá build needs) via .github/workflows/graph-rebuild.yml.
#
#   scripts/rebuild-deploy.sh bogota
#   DRY_RUN=1   scripts/rebuild-deploy.sh bogota   # build + publish nothing, print what would deploy
#   SKIP_BUILD=1 scripts/rebuild-deploy.sh bogota  # reuse an existing data/<city>/graph.obj
#   NO_PUSH=1   scripts/rebuild-deploy.sh bogota   # bump the yaml but do not git commit/push
#
# Needs:
#   RAILWAY_TOKEN_PROD   Railway token for the production environment. Use a DURABLE account/team
#                        token: project tokens have died on us mid-month (see the graph-drift notes),
#                        and a dead token here is a half-applied rebuild.
#   gh                   authenticated to the org (release create/upload); in CI the runner's gh.
# Optional:
#   OTP_HEAP (14G)  JAVA (path to a JDK >= 21)  OTP_SERVICE (otp-<city>)  API_SERVICE (api)
#   RAILWAY_ENV (production)  OPS_TELEGRAM_BOT_TOKEN + OPS_TELEGRAM_CHAT_ID (before/after ping)
set -euo pipefail

CITY="${1:?usage: rebuild-deploy.sh <city>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OTP_SERVICE="${OTP_SERVICE:-otp-$CITY}"      # prod runs one otp service per city; Bogotá's is otp-bogota
API_SERVICE="${API_SERVICE:-api}"
RAILWAY_ENV="${RAILWAY_ENV:-production}"
BASE="${API_BASE:-https://api.opentransit.tech}"
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo jeronimotech/opentransit-api)"
TAG="graph-$CITY-$(date +%F)"
DATA="$ROOT/data/$CITY"

say() { printf '\n\033[1m[rebuild:%s]\033[0m %s\n' "$CITY" "$*"; }

# A single before/after line to Telegram, if a channel is set. Never echoes the token.
notify() {
  [ -n "${OPS_TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${OPS_TELEGRAM_CHAT_ID:-}" ] || return 0
  curl -sS --max-time 10 -o /dev/null \
    "https://api.telegram.org/bot${OPS_TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${OPS_TELEGRAM_CHAT_ID}" \
    --data-urlencode "disable_web_page_preview=true" \
    --data-urlencode "text=$1" || true
}

# Pull drift% and rtById% straight from production's /health, for the before/after report.
health_line() {
  curl -sS --max-time 20 "$BASE/v1/cities/$CITY/health" 2>/dev/null | python3 -c "
import sys,json
try: d=json.load(sys.stdin)
except Exception: print('drift=? rtById=?'); raise SystemExit
g=(d.get('router') or {}).get('graphDrift') or {}
rt=d.get('realtime') or {}
print(f\"drift={g.get('overlapPct')}% rtById={rt.get('pctTripResolvedById')}%\")
" 2>/dev/null || echo "drift=? rtById=?"
}

[ -f "$ROOT/otp/$CITY/sources.env" ] || { echo "no otp/$CITY/sources.env — unknown city" >&2; exit 1; }

BEFORE="$(health_line)"
say "repo=$REPO  tag=$TAG  otp-service=$OTP_SERVICE  env=$RAILWAY_ENV"
say "before: $BEFORE"

# ---------------------------------------------------------------- 1. build (the 14 GB step)
if [ -n "${SKIP_BUILD:-}" ]; then
  [ -s "$DATA/graph.obj" ] || { echo "SKIP_BUILD set but no $DATA/graph.obj" >&2; exit 1; }
  say "reusing existing graph $(ls -lh "$DATA/graph.obj" | awk '{print $5}')"
else
  say "building graph (heap ${OTP_HEAP:-14G})…"
  OTP_HEAP="${OTP_HEAP:-14G}" ${JAVA:+JAVA="$JAVA"} scripts/build-graph.sh "$CITY"
fi

# ---------------------------------------------------------------- 2. baseline + checksum (§1)
say "writing trip-id baseline + checksum"
scripts/graph_drift.py --write-baseline "$DATA/$CITY-gtfs.zip" "$DATA/trip-ids.txt.gz"
( cd "$DATA" && shasum -a 256 graph.obj > SHA256SUMS )
GRAPH_SHA256="$(awk '{print $1}' "$DATA/SHA256SUMS")"
say "graph.obj sha256 = $GRAPH_SHA256"

BASE_DL="https://github.com/$REPO/releases/download/$TAG"
GRAPH_URL="$BASE_DL/graph.obj"
ROUTER_CONFIG_URL="$BASE_DL/router-config.json"
TRIP_IDS_URL="$BASE_DL/trip-ids.txt.gz"

if [ -n "${DRY_RUN:-}" ]; then
  say "DRY_RUN — would publish $TAG and deploy:"
  cat <<EOF
  GRAPH_URL         = $GRAPH_URL
  GRAPH_SHA256      = $GRAPH_SHA256
  ROUTER_CONFIG_URL = $ROUTER_CONFIG_URL
  trip_ids_url      = $TRIP_IDS_URL   (-> cities/$CITY.yaml)
  redeploy          = $OTP_SERVICE then $API_SERVICE ($RAILWAY_ENV)
EOF
  exit 0
fi

# ---------------------------------------------------------------- 3. publish the release (§1)
say "publishing release $TAG"
assets=(graph.obj build-config.json "$ROOT/otp/$CITY/router-config.json" trip-ids.txt.gz SHA256SUMS)
( cd "$DATA"
  if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
    gh release upload "$TAG" "${assets[@]}" --repo "$REPO" --clobber
  else
    gh release create "$TAG" "${assets[@]}" --repo "$REPO" \
      --title "$CITY OTP graph $(date +%F)" --notes "OTP 2.9.0 graph for $CITY. Published by scripts/rebuild-deploy.sh."
  fi )

# ---------------------------------------------------------------- 4. point the otp service at it (§6)
: "${RAILWAY_TOKEN_PROD:?set RAILWAY_TOKEN_PROD (durable Railway token for production)}"
say "setting GRAPH_URL/GRAPH_SHA256/ROUTER_CONFIG_URL on $OTP_SERVICE"
RAILWAY_TOKEN="$RAILWAY_TOKEN_PROD" railway variables -s "$OTP_SERVICE" -e "$RAILWAY_ENV" \
  --set "GRAPH_URL=$GRAPH_URL" --set "GRAPH_SHA256=$GRAPH_SHA256" --set "ROUTER_CONFIG_URL=$ROUTER_CONFIG_URL"

# ---------------------------------------------------------------- 5. bump trip_ids_url in the repo
# The API only learns the new baseline when it is redeployed from a commit that carries the new URL;
# until then /health grades the new graph against the old ids and reports a number that means nothing.
say "pointing cities/$CITY.yaml at $TAG"
python3 - "$CITY" "$TRIP_IDS_URL" <<'PY'
import pathlib, re, sys
city, url = sys.argv[1], sys.argv[2]
p = pathlib.Path("cities") / f"{city}.yaml"
s = p.read_text()
# Replace only the value on the single `trip_ids_url:` line, keeping its indentation.
new, n = re.subn(r"^(\s*trip_ids_url:\s*).*$", lambda m: m.group(1) + url, s, count=1, flags=re.M)
if n != 1:
    sys.exit(f"expected exactly one trip_ids_url line in {p}, found {n}")
p.write_text(new)
print(f"  trip_ids_url -> {url}")
PY

if [ -z "${NO_PUSH:-}" ]; then
  if ! git diff --quiet -- "cities/$CITY.yaml"; then
    git add "cities/$CITY.yaml"
    git commit -m "graph($CITY): point trip_ids_url at $TAG"
    git push
  else
    say "cities/$CITY.yaml already at this tag — nothing to commit"
  fi
else
  say "NO_PUSH set — left cities/$CITY.yaml edited, uncommitted"
fi

# ---------------------------------------------------------------- 6. redeploy both services (§6 + §1)
say "redeploying $OTP_SERVICE (graph re-download) then $API_SERVICE (new baseline)"
RAILWAY_TOKEN="$RAILWAY_TOKEN_PROD" railway up -s "$OTP_SERVICE" -e "$RAILWAY_ENV" -d -c
RAILWAY_TOKEN="$RAILWAY_TOKEN_PROD" railway up -s "$API_SERVICE" -e "$RAILWAY_ENV" -d -c

# ---------------------------------------------------------------- 7. verify (best effort)
# otp downloads then loads the graph (~2–4 min) and the api re-ingests the static feed after it
# redeploys; every city reads enabled:false for the first minute or two (bootstrap, not a failure).
say "waiting for the new graph to load + re-ingest (up to ~8 min)…"
AFTER="$BEFORE"; ok=""
for _ in $(seq 1 16); do
  sleep 30
  AFTER="$(health_line)"
  drift="$(printf '%s' "$AFTER" | sed -E 's/.*drift=([0-9.]+)%.*/\1/')"
  case "$drift" in
    ''|*[!0-9.]*) : ;;                              # not a number yet (bootstrap / parse miss)
    *) awk "BEGIN{exit !($drift >= 90)}" && { ok=1; break; } ;;
  esac
done
say "after: $AFTER"
[ -n "$ok" ] && say "drift back above 90% ✓" || say "drift not confirmed >90% yet — check /health in a few minutes"

notify "✅ ${CITY}: graph rebuilt & deployed ($TAG). before[$BEFORE] after[$AFTER]. Confirm a number only the new graph has (a route's pattern count), not just drift=100%."
say "done. Sanity-check a value only the NEW graph carries (e.g. a route's pattern count), per the graph-drift runbook — drift=100% alone only proves the API re-graded."
