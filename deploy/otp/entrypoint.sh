#!/bin/bash
# Fetch the prebuilt graph into the data dir (a persistent volume in production), then serve it.
#   GRAPH_URL          (required) URL of graph.obj built with the same OTP version
#   GRAPH_SHA256       (optional) expected checksum; a mismatch or a new value forces a re-download
#   ROUTER_CONFIG_URL  (optional) URL of router-config.json, refreshed on every boot
#   OTP_DATA_DIR       (default /var/opentripplanner)   PORT (default 8080)   JAVA_OPTS (default -Xmx5G)
set -euo pipefail
DIR="${OTP_DATA_DIR:-/var/opentripplanner}"; mkdir -p "$DIR"
: "${GRAPH_URL:?GRAPH_URL is required}"
need=1
if [ -f "$DIR/graph.obj" ]; then
  if [ -n "${GRAPH_SHA256:-}" ]; then
    have=$(sha256sum "$DIR/graph.obj" | awk '{print $1}')
    [ "$have" = "$GRAPH_SHA256" ] && need=0 || echo "[otp] graph checksum changed, re-downloading"
  else need=0; fi
fi
if [ "$need" = 1 ]; then
  echo "[otp] downloading graph from $GRAPH_URL"
  curl -fsSL --retry 5 --retry-delay 5 -o "$DIR/graph.obj.part" "$GRAPH_URL"
  if [ -n "${GRAPH_SHA256:-}" ]; then
    have=$(sha256sum "$DIR/graph.obj.part" | awk '{print $1}')
    [ "$have" = "$GRAPH_SHA256" ] || { echo "[otp] checksum mismatch: $have" >&2; exit 1; }
  fi
  mv "$DIR/graph.obj.part" "$DIR/graph.obj"
  echo "[otp] graph ready: $(du -h "$DIR/graph.obj" | cut -f1)"
fi
if [ -n "${ROUTER_CONFIG_URL:-}" ]; then
  curl -fsSL --retry 5 -o "$DIR/router-config.json" "$ROUTER_CONFIG_URL" && echo "[otp] router-config refreshed"
fi
echo "[otp] serving on port ${PORT:-8080} with JAVA_OPTS=${JAVA_OPTS:-}"
exec /docker-entrypoint.sh --load --port "${PORT:-8080}" "$@"
