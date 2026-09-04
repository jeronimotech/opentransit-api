# opentransit-api

Open-source, **multi-city**, **multimodal** trip-planning backend: static GTFS + GTFS-Realtime +
[OpenTripPlanner 2](https://www.opentripplanner.org/), behind one small FastAPI service with a clean,
app-friendly JSON contract. First city: **Bogotá** (TransMilenio troncal, alimentadores, dual, SITP zonal,
TransMiCable).

It is the backend of the `opentransit` family (`opentransit-web`, `opentransit-mobile`). Anyone can run it
for their own city with two config files and a GTFS feed — no API keys, no proprietary services.

```
GET /v1/cities/bogota/plan?fromLat=4.7546&fromLon=-74.0459&toLat=4.5978&toLon=-74.1616
→ 5 itineraries · legs with realtime delays · encoded geometries · alerts, in ~1 s
```

## Architecture

```mermaid
flowchart LR
  subgraph upstream["Per city: open data"]
    GTFS[GTFS.zip]
    RT[GTFS-RT<br/>positions · tripupdates · alerts]
    OSM[OpenStreetMap extract]
  end
  subgraph otp["OpenTripPlanner 2.9 (one per city)"]
    GRAPH[graph.obj]
    UPD[RT updaters]
  end
  subgraph api["opentransit-api (FastAPI)"]
    REG[City registry<br/>cities/*.yaml]
    POLL[GTFS-RT poller<br/>in-memory frame + deltas]
    ING[Static ingest<br/>routes · stops · shapes]
    NORM[Normalizer<br/>OTP → contract]
    PG[(PostGIS)]
  end
  WEB[opentransit-web]
  MOB[opentransit-mobile]
  PHOTON[Photon geocoder]

  GTFS --> GRAPH
  OSM --> GRAPH
  RT --> UPD
  RT --> POLL
  GTFS --> ING --> PG
  GRAPH -->|GraphQL /otp/gtfs/v1| NORM
  PG --> NORM
  POLL --> NORM
  PHOTON -.-> NORM
  REG --> NORM
  NORM -->|/v1/cities/{city}/…| WEB & MOB
```

- **One tenant = one city.** Every endpoint is `/v1/cities/{city}/…`. City config lives in `cities/<city>.yaml`
  (feeds, timezone, bbox, agencies → components, OTP URL). Nothing in `app/` knows about Bogotá.
- **OTP does the routing and the timetable.** The API never exposes raw OTP; `app/normalize.py` turns
  `planConnection`, stops, routes and alerts into the contract in [`docs/API.md`](docs/API.md).
- **The API does realtime fan-out.** One poll of each GTFS-RT feed every ~15 s serves every client:
  `/vehicles` (snapshot), `/vehicles/stream` (SSE, gzip-flushed deltas), `/alerts`, vehicle trails.
- **PostGIS holds the light part of the feed.** Routes, trips, stops (geography), simplified shapes,
  stop→routes. `stop_times.txt` (92 % of a big feed) is streamed once and never stored.
- **Search without keys.** Stops/stations from PostGIS (trigram + unaccent) merged with
  [Photon](https://photon.komoot.io) for addresses and POIs, restricted to the city bbox.

## Quickstart (Bogotá, ~5 minutes + downloads)

Requirements: Docker, Python ≥ 3.12, a JDK ≥ 21 (`brew install openjdk`) — OTP runs natively on macOS
because Docker Desktop's VM is usually too small for a big-city graph build.

```bash
git clone … opentransit-api && cd opentransit-api
make venv                       # .venv with dev deps
make up                         # Postgres/PostGIS on localhost:5435
make graph CITY=bogota          # GTFS (118 MB) + Colombia OSM (330 MB) → clip → OTP graph (≈ 2 min, 178 MB)
make otp                        # serve the graph on http://localhost:8080 (6 GB heap)
cp .env.example .env
make dev                        # API on http://localhost:8001 — first start ingests the static feed (~1 min)
```

Then:

```bash
curl localhost:8001/healthz
curl "localhost:8001/v1/cities/bogota/plan?fromLat=4.7546&fromLon=-74.0459&toLat=4.5978&toLon=-74.1616" | jq '.itineraries[0]'
curl "localhost:8001/v1/cities/bogota/geocode?q=portal"
curl "localhost:8001/v1/cities/bogota/stops/nearby?lat=4.6534&lon=-74.0836"
curl "localhost:8001/v1/cities/bogota/vehicles?bbox=-74.1,4.6,-74.0,4.7" | jq .count
curl -N "localhost:8001/v1/cities/bogota/vehicles/stream"      # SSE
open http://localhost:8001/docs
```

Stop / restart: `make otp-stop`, `make down`, `Ctrl-C` the API. Full Docker (Linux hosts with ≥ 12 GB for
the VM): `docker compose --profile full up -d` runs Postgres, `otp-bogota` and the API in containers
(build the graph first with `OTP_RUNTIME=docker make graph`).

## Endpoints (summary)

| | |
|---|---|
| `GET /healthz`, `GET /v1/cities`, `GET /v1/cities/{city}` | platform |
| `GET /v1/cities/{city}/plan` | itineraries (modes, arriveBy, wheelchair, numItineraries, locale) |
| `GET …/geocode?q=`, `GET …/reverse?lat&lon` | search |
| `GET …/stops/nearby`, `GET …/stops/{id}`, `GET …/stops/{id}/departures` | stops |
| `GET …/stops/{id}/board`, `GET …/stops/{id}/routes/{routeId}/next` | arrival board, "Ubica tu bus" (v1.1) |
| `GET …/pois?bbox=&type=` | station services layer from OSM (v1.1) |
| `GET …/routes`, `GET …/routes/{id}`, `GET …/network` | routes & map layer (network shapes are deduped server-side; `?all=true` for every shape) |
| `GET …/vehicles`, `GET …/vehicles/stream?bbox=&routeIds=` (SSE), `GET …/vehicles/{id}` | realtime |
| `GET …/alerts`, `GET …/health` | realtime & health |
| `POST /v1/admin/cities/{city}/ingest-static`, `…/purge` | admin (`X-Admin-Token`) |
| `GET /v1/admin/me`, `GET/PUT/DELETE …/admin/cities/{city}/config`, `…/config/history` | runtime-editable fares, client config, links, services (`X-Admin-Token`) |

Full schema and examples: [`docs/API.md`](docs/API.md). Errors are always
`{"error": {"code": "…", "message": "…"}}`.

### Editing fares and client config without a redeploy

Set `ADMIN_TOKEN` in `.env` (any long random string; the admin web UI sends it as `X-Admin-Token`).
`PUT /v1/admin/cities/{city}/config` deep-merges an override over the city YAML for `fares`, `config`,
`links`, `services` and `branding.primaryColor`; it is validated, stored in Postgres with a history, and
applied in memory at once — `/plan` estimates fares with the new values on the next request, and
`/v1/cities/{city}` serves them with `Cache-Control: max-age=60`, so cached clients catch up within a
minute. `DELETE` resets to the YAML. The YAML remains the base for new deployments and for everything
that is not in that list (feeds, OTP, agencies, bbox…).

## v1.1 features (from the TransMi App / Maas analysis)

| Feature | Endpoint / field |
|---|---|
| Estimated fare per itinerary ("Tarifa estimada") | `Itinerary.fare` from `city.fares` |
| Service hours per route ("Fuera de horario · próximo 04:30") | `RouteRef.serviceWindow` |
| Arrival board grouped by route with next 3 times | `GET /stops/{id}/board` |
| "Ubica tu bus": stop + route → next buses, live / estimated / scheduled | `GET /stops/{id}/routes/{routeId}/next` |
| Realtime freshness / stale flag | `health.realtime.stale`, `freshness` on board/next |
| Alert severity always present (inferred from effect when the feed omits it) | `Alert.severity`, `severitySource` |
| Station services layer from OSM (bike parking, toilets, ATMs, health, libraries…) | `GET /pois`, `scripts/build-pois.sh` |
| Honest accessibility ("no verificado" when the feed value is a constant) | `Stop.accessibility` |
| Server-side bbox / route filter on the vehicle stream | `GET /vehicles/stream?bbox=&routeIds=` |
| Nearby-first search | `GET /geocode?q=&lat=&lon=` |
| Remote config, component palette, official links, hand-off tiles | `city.config`, `components`, `links`, `services` |

Details and JSON shapes: [`docs/API.md`](docs/API.md) · plan: [`docs/ROADMAP-v1.1.md`](docs/ROADMAP-v1.1.md).


## v1.2 · shared bikes (GBFS) — provider-agnostic

Any bike/scooter-share system that publishes [GBFS](https://gbfs.org) plugs in with config only:

```yaml
# cities/<city>.yaml
mobility:
  bike_share:
    - { id: acme, name: Acme Bikes, network: acme_city,      # network = OTP updater id
        gbfs_url: https://acme.example/gbfs/gbfs.json, color: "#00A859", url: https://acme.example,
        apps: { ios: null, android: null }, pricing_summary: null, form_factors: [bicycle] }
```

Then `scripts/otp-updaters.py <city>` (run automatically by `scripts/otp-native.sh serve`) writes one OTP
`vehicle-rental` updater per network and you restart OTP. No graph rebuild. What you get:

- `/plan?modes=TRANSIT,WALK,BIKE_RENTAL` → itineraries with `rental` legs (pickup/drop-off station, live
  availability, price estimate), `rentalLegs`, `modesUsed`, fare breakdown with `kind: rental`.
- `/rental/networks`, `/rental/stations?bbox=`, `/rental/stations/{id}`, `stops/nearby?include=stops,rental`,
  `health.rental`.
- Several networks per city are supported (distinct ids/colours); the `mobility` section is also editable at
  runtime from the admin config. The API polls each feed itself (ttl-aware) and never proxies per client.

Bogotá ships with the city's public bike system (252 stations, GBFS 3.0) configured in `cities/bogota.yaml`.

## Add a city in five steps

> Bike-share? Add `mobility.bike_share[]` to the YAML and run `scripts/otp-updaters.py <city>` before serving OTP.

1. **Config:** copy `cities/_template.yaml` to `cities/<slug>.yaml`. Fill timezone, bbox, center, feed URLs,
   and map each `agency_id` from `agency.txt` to a component (`trunk|feeder|dual|zonal|cable|rail|other`)
   and a color — the apps color everything by component.
2. **OTP inputs:** create `otp/<slug>/sources.env` (`GTFS_URL`, `OSM_URL` from
   [Geofabrik](https://download.geofabrik.de), `BBOX`), `build-config.json` (set `feedId: <slug>`,
   `transitModelTimeZone`) and `router-config.json` (realtime updaters, or `"updaters": []`).
3. **Build & serve:** `make graph CITY=<slug>` then `scripts/otp-native.sh serve <slug> 8081`
   (or add an `otp-<slug>` service to `docker-compose.yml`). Point `otp.base_url` in the YAML at it
   (`${OTP_<SLUG>_URL:-http://localhost:8081}`).
4. **Run the API** — it discovers the city, ingests the static feed and starts the poller if RT URLs are set.
   Check `GET /v1/cities/<slug>/health`.
5. **Document the quirks** in `docs/cities/<slug>.md` (what the feed lacks: fares, transfers, pathways…)
   so app developers know what to expect. Open a PR.

Optional but recommended (v1.1): fill `components`, `fares` (flat-fare estimate; mark it "verify"), `config`,
`links` (official PQRS / recharge / support pages only) and `services` tiles in the YAML, then run
`scripts/build-pois.sh <slug>` and commit `cities/<slug>/pois.geojson`.

## Bogotá data notes (read before trusting the data)

Verified against the official feeds on 2026-09-04 (details in `docs/cities/bogota.md`):

- **Static GTFS** (`gtfs.transmilenio.gov.co/GTFS.zip`): 7 agencies, 1,052 routes, 8,335 stops,
  181k trips, 9.6 M stop_times, real shapes. One calendar for the whole year.
- **Realtime**: ~6,000 vehicles every 15 s. `TripUpdates` carries **one** stop_time_update per trip (the next
  stop): OTP propagates that delay backwards (`backwardsDelayPropagationType: ALWAYS`), so `realtime` is true
  only near the vehicle's current position — the API does not invent downstream ETAs.
- **~11 % of realtime `trip_id`s are not in the static feed** (`tripResolved: false`); those buses still show
  on the map with their route, but cannot feed itineraries.
- **No fares** (`fare: null` everywhere), **no `transfers.txt`**, **no `pathways.txt`**,
  `wheelchair_boarding=1` on every stop — the accessibility flag is a default, not data. `wheelchair=true`
  routing therefore reflects streets, not stations.

## Development

```bash
make test     # 23 unit tests, no network/DB (pytest)
make lint     # ruff
```

Layout: `app/` (service) · `cities/` (tenants) · `otp/<city>/` (router configs) · `scripts/` (graph build,
native OTP) · `docs/` (contract, per-city notes) · `tests/`.

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under [MIT](LICENSE); see [NOTICE.md](NOTICE.md) for
upstream credits (OpenTripPlanner, OpenStreetMap, Photon, TRANSMILENIO S.A., SIRCI Live).
