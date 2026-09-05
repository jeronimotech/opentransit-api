# Deploying a city on Railway

This is how the Bogotá sandbox is deployed. The same steps work for any city and for the
production environment: only the environment token and the graph release change.

## Architecture (one Railway project, one environment per stage)

```
                 ┌──────────────┐  https  ┌──────────────┐
  browser/app ──▶│  web (Next)  │────────▶│  api (FastAPI)│──▶ postgres (PostGIS, volume)
                 └──────────────┘         │              │──▶ otp (OpenTripPlanner, volume)
                                          │  GTFS-RT /   │        ▲ downloads graph.obj from a
                                          │  GBFS pollers│        │ GitHub Release on first boot
                                          └──────────────┘
```

| service | source | image / build | port | volume |
|---|---|---|---|---|
| `postgres` | Docker image | `postgis/postgis:16-3.4` | 5432 (private only) | `/var/lib/postgresql/data` |
| `otp` | this repo | `deploy/otp/Dockerfile` (`RAILWAY_DOCKERFILE_PATH`) | 8080 (private only) | `/var/opentripplanner` |
| `api` | this repo | `Dockerfile` (root) + `railway.json` health check `/healthz` | 8000 (public domain) | – |
| `web` | `opentransit-web` repo | `Dockerfile` (root) | 3000 (public domain) | – |

Services talk over Railway's private network (`<service>.railway.internal`). Only `api` and `web`
get public domains. Nothing is built on Railway except the two app images: the OTP graph is built
locally (or in CI) and published as a release asset.

## 0. Prerequisites

- Railway CLI ≥ 5 (`brew install railway`), a **project token** per environment (Project → Settings →
  Tokens). Export it as `RAILWAY_TOKEN` for every command below. Never commit it.
- `gh` CLI logged in to the GitHub org that hosts this repo.
- A graph built for the city: `scripts/otp-native.sh build <city>` (macOS) or `scripts/build-graph.sh <city>`.
  The graph must be built with the **same OTP version** the `otp` image runs (`2.9.0`).

## 1. Publish the graph as a release

```bash
CITY=bogota; TAG=graph-$CITY-$(date +%F)
cd data/$CITY && shasum -a 256 graph.obj > SHA256SUMS
gh release create $TAG graph.obj build-config.json ../../otp/$CITY/router-config.json SHA256SUMS \
  --title "$CITY OTP graph $(date +%F)" --notes "OTP 2.9.0 graph for $CITY"
```
Note the asset URLs: `https://github.com/<org>/opentransit-api/releases/download/$TAG/{graph.obj,router-config.json}`.

## 2. Create the services

```bash
export RAILWAY_TOKEN=<project token for the environment>
railway add --service postgres --image postgis/postgis:16-3.4 \
  --variables POSTGRES_USER=opentransit --variables "POSTGRES_PASSWORD=$(openssl rand -hex 16)" \
  --variables POSTGRES_DB=opentransit --variables PGDATA=/var/lib/postgresql/data/pgdata
railway add --service otp
railway add --service api
railway add --service web
```
Volumes (`railway volume add -m <path>` needs the directory linked to the service, or use the dashboard):
`postgres` → `/var/lib/postgresql/data`, `otp` → `/var/opentripplanner`.

## 3. Variables

| service | variable | value |
|---|---|---|
| otp | `GRAPH_URL` | release asset URL of `graph.obj` |
| otp | `GRAPH_SHA256` | from `SHA256SUMS` (changing it forces a re-download) |
| otp | `ROUTER_CONFIG_URL` | release asset URL of `router-config.json` (re-fetched on every boot) |
| otp | `JAVA_OPTS` | `-Xmx5G -XX:+UseParallelGC` (Bogotá needs ≥ 4 GB heap; the plan must allow ≥ 6 GB) |
| otp | `PORT` | `8080` |
| otp | `RAILWAY_DOCKERFILE_PATH` | `deploy/otp/Dockerfile` |
| api | `DATABASE_URL` | `postgresql://opentransit:<POSTGRES_PASSWORD>@postgres.railway.internal:5432/opentransit` |
| api | `OTP_<CITY>_URL` | `http://otp.railway.internal:8080` (referenced from `cities/<city>.yaml`) |
| api | `ADMIN_TOKEN` | `openssl rand -hex 32` — set it only in Railway, never in the repo |
| api | `CORS_ORIGINS` | `https://<web public domain>` (use `*` only while testing) |
| api | `LOG_JSON` / `LOG_LEVEL` | `true` / `INFO` |
| web | `NEXT_PUBLIC_API_URL` | `https://<api public domain>` (build-time: redeploy web after changing it) |
| web | `NEXT_PUBLIC_DEFAULT_CITY` | `<city>` |
| web | `NEXT_PUBLIC_ADMIN_ENABLED` | `1` to expose `/admin` |
| web | `PORT` | `3000` |

```bash
railway variables --service otp --set GRAPH_URL=... --set GRAPH_SHA256=... --set ROUTER_CONFIG_URL=... \
  --set "JAVA_OPTS=-Xmx5G -XX:+UseParallelGC" --set PORT=8080 --set RAILWAY_DOCKERFILE_PATH=deploy/otp/Dockerfile
railway domain -s api -p 8000        # prints the public domain
railway domain -s web -p 3000
```

## 4. Deploy

```bash
# from a clean checkout of opentransit-api
railway up -s otp -e <env> -d -c
railway up -s api -e <env> -d -c
# from a clean checkout of opentransit-web
railway up -s web -e <env> -d -c
```
`otp` downloads the graph into its volume on first boot (≈ 1–2 min) and then loads it (≈ 1–2 min).
`api` creates the schema, starts the GTFS-RT and GBFS pollers immediately and ingests the static GTFS
in the background (≈ 118 MB download; a few minutes). `web` is a static build against the API URL.

## 5. Verify

```bash
API=https://<api public domain>
curl -s $API/healthz
curl -s $API/v1/cities/<city>/health      # router.up, realtime.vehicles, rental.networks[].up
curl -s "$API/v1/cities/<city>/plan?fromLat=..&fromLon=..&toLat=..&toLon=.."
curl -sI https://<web public domain>/<city>
```

## 6. Updating the graph

Build a new graph, publish a new release (step 1), then set `GRAPH_URL` + `GRAPH_SHA256` on `otp`
(the checksum change triggers the re-download) and redeploy `otp`. `router-config.json` changes alone
only need `ROUTER_CONFIG_URL` to point at the new asset and a restart. Rebuild monthly: the graph is
bounded to −1 month / +6 months of transit service.

## 7. Promoting to production

Same steps with the **production** project token: create the four services in the `production`
environment (or duplicate the sandbox environment from the dashboard), attach the two volumes, set the
variables with a fresh `ADMIN_TOKEN` and `POSTGRES_PASSWORD`, point `NEXT_PUBLIC_API_URL` at the prod
API domain, tighten `CORS_ORIGINS` to the prod web domain, add custom domains if any, then `railway up`
each service. Keep the same graph release unless the feed changed.

## Operations notes

- Secrets live only in Railway variables (and your password manager). This repo never contains them.
- The `otp` service needs a plan that allows ≥ 6 GB RAM per service for Bogotá-sized graphs.
- Health checks: `api` uses `/healthz` (in `railway.json`). `otp` has none because its boot takes
  minutes; the API reports `router.up` instead.
- Logs: `railway logs -s <service> -e <env>` (`-b` for build logs).
