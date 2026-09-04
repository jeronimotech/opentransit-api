# opentransit-api — API contract (v1)

> Source of truth for the web and mobile apps. Deviations from the original shared contract are listed at the end.
> Interactive docs: `GET /docs` (OpenAPI) on the running service.

Open-source, multi-tenant (one tenant = one city), multimodal trip planner.
Three independent repos under `/Users/luisjavier/dev/projects/open-public-tp/`:

| repo | stack | port (dev) |
|---|---|---|
| `opentransit-api` | Python 3.12 FastAPI + PostgreSQL/PostGIS + OpenTripPlanner 2.9.0 | API 8001 · OTP 8080 · Postgres 5435 (host) |
| `opentransit-web` | Next.js 15 (App Router) + React 19 + TypeScript + Tailwind + MapLibre GL JS | 3000 |
| `opentransit-mobile` | Flutter 3.41 / Dart 3.11 + maplibre_gl + riverpod + go_router + dio | — |

License for all three: MIT. Language of code/comments/docs: English. UI copy: Spanish (es) default + English (en), via i18n.
Basemap (no API key): OpenFreeMap vector tiles, style `https://tiles.openfreemap.org/styles/liberty` (dark: `.../styles/dark`? -> use `liberty` only if dark unavailable; verify). Attribution "© OpenMapTiles © OpenStreetMap contributors".

## Tenancy model
- A **city** is the tenant. City config lives in the API (`cities/*.yaml` loaded at startup; DB not required for config).
- Every public endpoint is scoped: `/v1/cities/{city}/...`. `{city}` is a slug (`bogota`).
- Each city has its own OTP instance (container `otp-{city}`) and its own GTFS-RT poller inside the API process.
- Never hardcode Bogotá anywhere except `cities/bogota.yaml` and the Bogotá data-build script.

## Bogotá inputs (verified live 2026-09-04)
- GTFS static: `https://gtfs.transmilenio.gov.co/GTFS.zip` (~118 MB)
- GTFS-RT: `https://gtfs.transmilenio.gov.co/positions.pb`, `/tripupdates.pb`, `/alerts.pb` (refresh ~15 s, content-type text/plain, CORS null -> must be proxied)
- Quirks: TripUpdates has only ONE stop_time_update per trip (next stop); ~11% RT trip_ids unresolved vs static (fallback to route_id); no fares; no transfers.txt; wheelchair_boarding=1 everywhere (do not trust); agencies 1..7 = Troncal, Alimentadores, Dual, Zonal Urbano, Zonal Complementario, Zonal Especial, TransMiCable.
- OSM: Geofabrik `south-america/colombia-latest.osm.pbf`, clipped to bbox `-74.45,3.95,-73.85,4.90` with osmium (docker).
- Reusable prior code: `~/dev/projects/tmsa/2025/SIRCI/sirci-live-api/app/{rt.py,gtfs_static.py,geo.py,routers/realtime.py}` (already read; port and generalize, credit in NOTICE).

## Conventions
- JSON, camelCase keys. Times are ISO-8601 with offset (`2026-09-04T08:15:00-05:00`). Distances meters, durations seconds. Coordinates `{ "lat": 4.65, "lon": -74.08 }`.
- Errors: HTTP status + body `{ "error": { "code": "CITY_NOT_FOUND", "message": "..." } }`. Codes: `CITY_NOT_FOUND`, `STOP_NOT_FOUND`, `ROUTE_NOT_FOUND`, `VEHICLE_NOT_FOUND`, `BAD_REQUEST`, `ROUTER_UNAVAILABLE`, `NO_ITINERARIES`, `UNAUTHORIZED`.
- Pagination not needed in v1 (lists are bounded).
- CORS: allow all origins in dev; env-configurable.
- Cache headers: static-ish resources (`routes`, `network`, `cities`) `Cache-Control: public, max-age=3600`; realtime `no-store`.
- Polylines: Google encoded polyline, precision 1e-5, `{ "encoded": "...", "precision": 5 }`.

## Endpoints

### Platform
- `GET /healthz` → `{ "status": "ok", "version": "0.1.0", "cities": ["bogota"] }`
- `GET /v1/cities` → `{ "cities": [City] }`
- `GET /v1/cities/{city}` → `City`

```jsonc
City {
  "id": "bogota", "name": "Bogotá", "country": "CO", "timezone": "America/Bogota", "locale": "es-CO",
  "center": {"lat": 4.6534, "lon": -74.0836}, "bbox": [-74.45, 3.95, -73.85, 4.90], "defaultZoom": 12,
  "modes": ["WALK","BUS","CABLE_CAR","BICYCLE"],
  "branding": {"primaryColor": "#D32F2F", "logoUrl": null},
  "features": {"realtimeVehicles": true, "tripUpdates": true, "alerts": true, "fares": false, "bikeShare": false},
  "agencies": [{"id": "1", "name": "TransMilenio Troncal", "component": "trunk", "color": "#D32F2F"}],
  "attribution": "Datos: TRANSMILENIO S.A. (GTFS) · Mapa: © OpenMapTiles © OpenStreetMap contributors"
}
```
`component` values: `trunk | feeder | dual | zonal | cable | rail | other` (Bogotá mapping agency_id 1→trunk, 2→feeder, 3→dual, 4/5/6→zonal, 7→cable).

### Trip planning
- `GET /v1/cities/{city}/plan`
  - query: `fromLat, fromLon, toLat, toLon` (required); `fromName`, `toName` (optional labels, echoed into `from.name`/`to.name` and the first/last walking leg; when absent the API tries a reverse geocode within a 1.5 s budget, otherwise the name stays `null`); `time` (ISO-8601, default now); `arriveBy` (bool, default false); `modes` (comma list of `WALK,BUS,RAIL,SUBWAY,TRAM,CABLE_CAR,BICYCLE,TRANSIT`; default `TRANSIT,WALK`); `wheelchair` (bool); `numItineraries` (1..10, default 5); `maxWalkDistance` (m, default 1500); `locale` (`es|en`).
  - response `{ "from": Place, "to": Place, "itineraries": [Itinerary], "router": {"engine": "otp", "version": "2.10.0", "realtime": true}, "warnings": [string] }`
  - 502 `ROUTER_UNAVAILABLE` if OTP down; 200 with empty `itineraries` + warning `NO_ITINERARIES` is NOT an error.

```jsonc
Itinerary { "id": "it-0", "startTime": "...", "endTime": "...", "durationSeconds": 2700, "walkDistanceMeters": 800,
  "walkTimeSeconds": 600, "waitingTimeSeconds": 300, "transfers": 1, "fare": null | {"amount": 3200, "currency": "COP"},
  "accessible": null | true | false, "legs": [Leg] }
Leg { "mode": "WALK"|"BUS"|"RAIL"|"SUBWAY"|"TRAM"|"CABLE_CAR"|"BICYCLE"|"CAR"|"FERRY",
  "transit": bool, "startTime": "...", "endTime": "...", "durationSeconds": 900, "distanceMeters": 5200,
  "from": Place, "to": Place, "route": RouteRef|null, "headsign": string|null, "agency": {"id": "1", "name": "..."}|null,
  "tripId": string|null, "realtime": bool, "realtimeState": "SCHEDULED"|"UPDATED"|"CANCELED"|"ADDED"|"MODIFIED"|null,
  "delaySeconds": int|null, "geometry": {"encoded": "...", "precision": 5},
  "intermediateStops": [Place], "steps": [WalkStep], "alerts": [Alert] }
Place { "name": "Portal Norte", "lat": 4.75, "lon": -74.04, "stopId": "bogota:1234"|null, "stopCode": string|null,
  "arrival": "..."|null, "departure": "..."|null, "component": string|null }
RouteRef { "id": "bogota:B12", "shortName": "B12", "longName": "Portal Norte - ...", "color": "#D32F2F", "textColor": "#FFFFFF",
  "mode": "BUS", "agencyId": "1", "component": "trunk" }
WalkStep { "instruction": "Gira a la derecha en Calle 26", "distanceMeters": 120, "lat": .., "lon": .., "relativeDirection": "RIGHT"|"LEFT"|"CONTINUE"|"DEPART"|... , "streetName": "Calle 26" }
```
Stop/route ids are **feed-scoped** exactly as OTP exposes them (`bogota:<gtfs_id>`). Apps treat them as opaque strings.

### Geocoding / search
- `GET /v1/cities/{city}/geocode?q=portal%20norte&lat=&lon=&limit=8`
  - Sources merged: GTFS stops/stations (local DB, prefix + fuzzy, stations ranked above stops) and Photon (`https://photon.komoot.io/api/?q=&lat=&lon=&limit=&bbox=` restricted to city bbox; configurable `geocoder.photonUrl`, can be null to disable).
  - `{ "results": [ {"id": "stop:bogota:1234"|"photon:...", "name": "Portal Norte", "label": "Estación troncal · Autopista Norte", "lat": .., "lon": .., "type": "station"|"stop"|"address"|"poi"|"street"|"place", "stopId": string|null, "component": string|null, "source": "gtfs"|"photon"} ] }`
- `GET /v1/cities/{city}/reverse?lat=&lon=` → `{ "name": "Calle 26 # 13-19", "lat": .., "lon": .. }` (Photon reverse; falls back to nearest stop name).

### Stops
- `GET /v1/cities/{city}/stops/nearby?lat=&lon=&radius=500&limit=30` → `{ "stops": [Stop & {"distanceMeters": 120}] }` (`distanceMeters` is an integer)
- `GET /v1/cities/{city}/stops/{stopId}` → `Stop & { "routes": [RouteRef], "parentStation": Stop|null, "children": [Stop] }`
- `GET /v1/cities/{city}/stops/{stopId}/departures?limit=20&minutes=60` → `{ "stop": Stop, "generatedAt": "...", "departures": [Departure] }`
  - Works for parent stations too (`locationType: "station"`): departures are aggregated across the station's child stops, deduplicated by `tripId` and sorted by effective time. `GET /stops/{stationId}` lists the children in `children`, and every child carries `parentStationId`.
```jsonc
Stop { "id": "bogota:1234", "code": "A123"|null, "name": "...", "lat": .., "lon": .., "locationType": "stop"|"station"|"entrance",
  "component": string|null, "wheelchair": "unknown"|"accessible"|"not_accessible", "parentStationId": string|null }
Departure { "route": RouteRef, "headsign": "...", "tripId": "...", "scheduledTime": "...", "realtimeTime": "..."|null,
  "realtime": bool, "delaySeconds": int|null, "canceled": bool, "vehicleId": string|null, "stopSequence": int|null }
```
Departures come from OTP (`stopTimes` with RT applied). Because Bogotá TripUpdates only carry the next stop, `realtime` will be true only for the imminent arrival; do not fake others.

### Routes & network
- `GET /v1/cities/{city}/routes?component=&q=` → `{ "routes": [RouteRef] }`
- `GET /v1/cities/{city}/routes/{routeId}` → `RouteRef & { "patterns": [ {"id": "...", "headsign": "...", "directionId": 0|1|null, "geometry": {...}, "stops": [Stop] } ], "alerts": [Alert] }`
  - `headsign` falls back to the name of the pattern's last stop when the feed has none (Bogotá feeders); `directionId` is `null` when the feed does not set it.
- `GET /v1/cities/{city}/network` → `{ "feedVersion": "...", "shapes": [ {"id": "...", "routeId": "...", "component": "trunk", "color": "#..", "geometry": {"encoded": "...", "precision": 5}} ] }` (Douglas-Peucker ~20 m simplified, cache 1h; for the map "network" layer)

### Realtime vehicles
- `GET /v1/cities/{city}/vehicles?routeId=&component=&bbox=minLon,minLat,maxLon,maxLat` → `VehicleFrame`
- `GET /v1/cities/{city}/vehicles/stream?deltas=true` → SSE, first event full `VehicleFrame`, then `{ "type": "delta", ..., "updated": [Vehicle], "removed": [ids] }`; keep-alive comments every 25 s; gzip via zlib sync-flush (see SIRCI realtime.py).
- `GET /v1/cities/{city}/vehicles/{vehicleId}` → `Vehicle & { "route": RouteRef|null, "trip": {"id": .., "resolved": bool, "headsign": ..}, "shape": {"encoded": ..}|null, "currentStop": Stop|null, "nextStop": Stop|null, "etaSeconds": int|null, "delaySeconds": int|null, "history": {"points": [[lon,lat,ts]], "avgKmh": ..}, "alerts": [Alert] }`
```jsonc
VehicleFrame { "type": "full", "seq": 123, "generatedAt": "...", "feedTimestamp": "...", "count": 6500,
  "health": {"entityAgeP50Seconds": 20, "pctTripResolved": 89.1, "httpStatus": 200}, "vehicles": [Vehicle] }
Vehicle { "id": "V123", "label": "Z12-3456"|null, "routeId": "bogota:B12"|null, "routeShortName": "B12"|null, "tripId": ..|null,
  "tripResolved": bool, "component": "trunk", "lat": .., "lon": .., "bearing": number|null, "timestamp": "...",
  "stopId": ..|null, "stopSequence": int|null, "occupancy": "EMPTY"|"MANY_SEATS_AVAILABLE"|...|null }
```

### Alerts & health
- `GET /v1/cities/{city}/alerts?routeId=&stopId=&active=true` → `{ "alerts": [Alert] }`
```jsonc
Alert { "id": "...", "cause": "UNKNOWN_CAUSE"|..., "effect": "DETOUR"|..., "severity": "INFO"|"WARNING"|"SEVERE"|null,
  "header": "...", "description": "..."|null, "url": ..|null, "start": "..."|null, "end": "..."|null,
  "routeIds": [..], "stopIds": [..], "routes": [RouteRef] }
```
- `GET /v1/cities/{city}/health` → `{ "static": {"feedVersion": "...", "fetchedAt": "...", "routes": 1024, "stops": 8309}, "realtime": {"lastFetchAt": "...", "entityAgeP50Seconds": .., "vehicles": .., "pctTripResolved": .., "alerts": ..}, "router": {"up": true, "version": "2.10.0", "graphBuiltAt": "..."} }`

### Admin (header `X-Admin-Token`)
- `POST /v1/admin/cities/{city}/ingest-static?force=true`
- `POST /v1/admin/cities/{city}/purge`

## OTP integration
- OTP 2.10 Docker image `opentripplanner/opentripplanner:2.10.0_2026-09-04T13-20` (pin), graph built once with `--build --save`, served with `--load`. GraphQL endpoint `POST http://otp-bogota:8080/otp/gtfs/v1` (GTFS GraphQL API; verify exact path/schema for 2.10 via docs `https://docs.opentripplanner.org/`).
- `router-config.json` updaters: `stop-time-updater` (tripupdates.pb, 20s, feedId `bogota`, `backwardsDelayPropagationType: ALWAYS`), `real-time-alerts` (alerts.pb, 60s), `vehicle-positions` (positions.pb, 20s). `build-config.json`: `transitFeeds: [{type: gtfs, feedId: bogota, source: file:///var/opentripplanner/bogota-gtfs.zip}]`, `osm: [{source: file:///var/opentripplanner/bogota.osm.pbf}]`, `transitModelTimeZone: America/Bogota`. Memory: `JAVA_TOOL_OPTIONS=-Xmx10G` for build, `-Xmx6G` serve.
- API translates `plan` → OTP `planConnection` GraphQL and normalizes to the schema above. Never expose raw OTP responses.

## Web app (opentransit-web) scope v1
Routes: `/` (city picker → redirect if only one) · `/{city}` (map + planner) · `/{city}/stops/{stopId}` · `/{city}/routes/{routeId}` · `/{city}/live` (vehicles) · `/{city}/alerts` · `/about`.
Planner: origin/destination inputs with geocode autocomplete + "use my location" + click-on-map; depart/arrive time; mode toggles; wheelchair toggle; itinerary cards (time, duration, transfers, walk, mode chips colored by route); itinerary detail with legs + steps; map draws legs, stops, live vehicles on the itinerary's routes. Share URL with query params. PWA manifest. i18n es/en. Dark mode.

## Mobile app (opentransit-mobile) scope v1
Screens: city picker (first launch, remembered) · Home map (nearby stops, live vehicles toggle, search bar) · Plan trip sheet · Results list · Itinerary detail (map + timeline) · Stop detail (departures, auto-refresh 20s) · Route detail · Alerts · Favorites (stops, routes, places; local storage) · Settings (city, language, accessibility, theme). Location via geolocator. Deep links `opentransit://{city}/plan?...`.

## Implementation notes & deviations (what this repo actually does)

| Topic | Contract said | Implemented |
|---|---|---|
| Dev ports | API 8000, Postgres 5433 | **API 8001** (8000 and 5433 were busy on the dev machine), **Postgres 5435**. Both are env-configurable (`API_HOST_PORT`, `POSTGRES_HOST_PORT`). |
| OTP version | 2.10 snapshot Docker image | **OTP 2.9.0** (released jar from Maven Central, also a Docker tag). Runs natively on macOS via `scripts/otp-native.sh`; Docker on Linux. The 2.10-only `Alert.activityPeriods` field is replaced by `effectiveStartDate/EndDate`. |
| `maxWalkDistance` | hard cap | OTP 2 has no hard walk cap. The value is mapped to walking reluctance (1500 m → 2.0; longer → lower reluctance). Accepted and documented, not enforced. |
| `modes=TRANSIT,BICYCLE` | bike access/egress | Access/egress are always WALK (Bogotá trips have no `bikes_allowed`, OTP would return nothing). BICYCLE becomes a direct bike-only itinerary offered next to the transit ones. |
| `wheelchair=true` | – | Works, but only for streets: `otp/bogota/router-config.json` sets `onlyConsiderAccessible: false` for trips and stops because the feed has no real accessibility data. Apps should show a "based on street data only" note. |
| Direct-only searches | – | OTP caps direct street searches at `maxDirectStreetDuration` (3 h here); a 20 km walk returns `NO_ITINERARIES`. |
| Vehicle history | Postgres series | **In-memory ring buffer** per vehicle (`VEHICLE_HISTORY_POINTS`, default 60 ≈ 15 min). Survives only while the process runs. |
| `Vehicle.occupancy` | optional | Filled only when the feed sends `occupancy_status` (Bogotá does not). |
| `Place.component` | any Place | Only set on stops that belong to a transit leg (component of that leg's route); origin/destination and intermediate walk points are `null`. |
| `Stop.component` | – | Dominant component of the routes serving the stop, learned by streaming `stop_times.txt` once at ingest (`INGEST_STOP_ROUTES`). Stations (`location_type=1`) have `null` unless they appear directly in stop_times. |
| `Departure.vehicleId` | – | Filled from our own RT frame (the bus currently running that trip), not from OTP. |
| `geocode` result `type` | station/stop/address/poi/street | adds `place` for Photon hits that are none of the above (neighbourhoods, localities). |
| `GET /v1/cities/{city}/health` `router.graphBuiltAt` | – | OTP 2.9 does not expose the graph build time on `GET /otp/`; returns `null`. `version` is filled. |
| `Alert.routes` | RouteRef list | Filled for alerts served from our RT cache (`/alerts`, `/vehicles/{id}`); empty for alerts embedded in OTP legs/routes (ids are still in `routeIds`). |
| Admin `purge` | DB purge | Clears in-memory vehicle history (there is no DB series to purge). |
| Time zone of timestamps | offset | Realtime timestamps (`generatedAt`, `Vehicle.timestamp`, alert `start`/`end`, departures) are UTC `Z`; OTP itinerary times carry the city offset. Both are valid ISO-8601. |
