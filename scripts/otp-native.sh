#!/usr/bin/env bash
# Run OpenTripPlanner natively (no Docker) — the right choice on macOS, where Docker Desktop's VM is
# usually capped at a few GB and a big-city graph build gets OOM-killed.
#   scripts/otp-native.sh build <city>          # builds data/<city>/graph.obj
#   scripts/otp-native.sh serve <city> [port]   # serves it (default port 8080), logs to data/<city>/otp.log
#   scripts/otp-native.sh stop  <city>
# Env: OTP_VERSION (2.9.0), OTP_HEAP (build 14G / serve 6G), JAVA (auto-detected JDK >= 21)
set -euo pipefail
CMD="${1:?build|serve|stop}"; CITY="${2:?city}"; PORT="${3:-8080}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OTP_VERSION="${OTP_VERSION:-2.9.0}"
JAR="$ROOT/vendor/otp-shaded-$OTP_VERSION.jar"
DATA="$ROOT/data/$CITY"
JAVA="${JAVA:-$(command -v java || true)}"
for cand in /opt/homebrew/opt/openjdk/bin/java /opt/homebrew/opt/openjdk@21/bin/java /usr/lib/jvm/java-21-openjdk/bin/java; do
  if [ -z "$JAVA" ] || ! "$JAVA" -version >/dev/null 2>&1; then [ -x "$cand" ] && JAVA="$cand"; fi
done
"$JAVA" -version >/dev/null 2>&1 || { echo "no working JDK found; set JAVA=/path/to/java" >&2; exit 1; }
if [ ! -f "$JAR" ]; then
  mkdir -p "$ROOT/vendor"
  echo "[otp] downloading otp-shaded-$OTP_VERSION.jar from Maven Central"
  curl -fsSL --retry 3 -o "$JAR" "https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/$OTP_VERSION/otp-shaded-$OTP_VERSION.jar"
fi
PIDFILE="$DATA/otp.pid"
case "$CMD" in
  build)
    cp "$ROOT/otp/$CITY/build-config.json" "$ROOT/otp/$CITY/router-config.json" "$DATA/"
    # build-config.json points at file:///var/opentripplanner/...; natively the base dir is data/<city>
    sed -i.bak 's|file:///var/opentripplanner/|file://'"$DATA"'/|g' "$DATA/build-config.json" && rm -f "$DATA/build-config.json.bak"
    t0=$(date +%s)
    "$JAVA" -Xmx"${OTP_HEAP:-14G}" -jar "$JAR" --build --save "$DATA"
    echo "[otp] graph built in $(( $(date +%s) - t0 )) s -> $(ls -lh "$DATA/graph.obj" | awk '{print $5}')"
    ;;
  serve)
    [ -f "$DATA/graph.obj" ] || { echo "no graph at $DATA/graph.obj — run build first" >&2; exit 1; }
    # vehicle-rental (GBFS) updaters are generated from cities/<city>.yaml: config only, never hand-edited
    "$ROOT/.venv/bin/python" "$ROOT/scripts/otp-updaters.py" "$CITY" 2>/dev/null || python3 "$ROOT/scripts/otp-updaters.py" "$CITY"
    cp "$ROOT/otp/$CITY/router-config.json" "$DATA/"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then echo "[otp] already running (pid $(cat "$PIDFILE"))"; exit 0; fi
    nohup "$JAVA" -Xmx"${OTP_HEAP:-6G}" -jar "$JAR" --load --port "$PORT" "$DATA" > "$DATA/otp.log" 2>&1 &
    echo $! > "$PIDFILE"
    echo "[otp] serving $CITY on http://localhost:$PORT (pid $!) · log: $DATA/otp.log"
    ;;
  stop)
    [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null && rm -f "$PIDFILE" && echo "[otp] stopped" || echo "[otp] not running"
    ;;
  *) echo "unknown command $CMD" >&2; exit 1 ;;
esac
