# opentransit-api — API contract (v1.1)

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
- `GET /v1/cities/{city}/network?all=false` → `{ "feedVersion": "...", "count": 719, "shapes": [ {"id": "...", "routeId": "bogota:12873", "routeIds": ["bogota:12873", "bogota:12874"], "component": "trunk", "color": "#..", "directionId": 0|1|null, "lengthMeters": 6700, "geometry": {"encoded": "...", "precision": 5}} ] }` (Douglas-Peucker ~20 m simplified, cache 1h; for the map "network" layer).
  **Server-side dedupe (v1.1.1):** feeds like Bogotá's model one commercial route as many GTFS routes with near-identical shapes. At ingest, shapes are grouped by component + route short name (+ `direction_id` when the feed has it); exact duplicates collapse, the longest shape is kept, and any other shape ≥ 90 % covered (points within 30 m) by an already-kept shape of the group is marked non-canonical. Only canonical shapes are returned; `routeIds` lists every route a shape stands for, so clients can still match a selected route. `?all=true` returns all shapes with `canonical` and `canonicalId` for debugging. Bogotá: 1,052 → 719 shapes (dual 715 → 523, feeder 179 → 72, trunk 113 → 98, zonal 44 → 25).

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
- `GET /v1/admin/me` → `{ "ok": true, "cities": ["bogota"] }` (token check for the admin UI login)

### Admin configuration (v1.1.1) — editable at runtime, no redeploy
The city YAML stays the base. Admins can override these sections only: `fares`, `config` (vehiclePollSeconds, departuresRefreshSeconds, features, minAppVersion, maintenance), `links`, `services`, `branding.primaryColor`. The override is stored in Postgres (`city_config_override` + `city_config_history`), deep-merged over the YAML, validated strictly, and swapped into memory, so `/v1/cities/{city}` and the `/plan` fare estimate reflect it immediately. Public city endpoints are served with `Cache-Control: public, max-age=60`, so cached clients see changes within a minute.
- `GET /v1/admin/cities/{city}/config` → `{ "effective": City, "override": {...}|null, "yaml": {fares, config, links, services, branding}, "revision": n, "updatedAt": "...", "updatedBy": "...", "editable": ["fares","config","links","services","branding"] }`
- `PUT /v1/admin/cities/{city}/config` body `{ "fares"?: {...}, "config"?: {...}, "links"?: {...}, "services"?: [...], "branding"?: {"primaryColor"}, "note"?: string, "updatedBy"?: string }` → partial deep-merge into the override (dicts merge, lists replace, a JSON `null` for a section or key removes that override so the YAML applies again). Validation runs on the *effective* result before anything is saved; errors use the standard envelope with the field path, e.g. `fares.maxTransfers: Input should be less than or equal to 5`. Rules: fares `currency` = 3 uppercase letters, `base`/`transfer` ≥ 0, `transferWindowMinutes` 0..600, `maxTransfers` 0..5, `note` ≤ 300; config poll/refresh seconds 5..120, `minAppVersion` semver `x.y.z`, `maintenance.message` ≤ 500; links https or null; services `id` slug, `label` ≤ 60, `icon` ∈ card|report|help|link|bike|parking|taxi|ticket|info|map, `url` https, `kind` external|internal|deeplink, unique ids; branding `primaryColor` `#RRGGBB`. Unknown sections (e.g. `feeds`) are rejected. Returns the GET shape; writes a history row.
- `DELETE /v1/admin/cities/{city}/config?updatedBy=` → clears the override (history row with note `reset`).
- `GET /v1/admin/cities/{city}/config/history?limit=20` → `{ "items": [ {"revision", "changedAt", "changedBy", "note", "data"} ] }` newest first.

## OTP integration
- OTP 2.10 Docker image `opentripplanner/opentripplanner:2.10.0_2026-09-04T13-20` (pin), graph built once with `--build --save`, served with `--load`. GraphQL endpoint `POST http://otp-bogota:8080/otp/gtfs/v1` (GTFS GraphQL API; verify exact path/schema for 2.10 via docs `https://docs.opentripplanner.org/`).
- `router-config.json` updaters: `stop-time-updater` (tripupdates.pb, 20s, feedId `bogota`, `backwardsDelayPropagationType: ALWAYS`), `real-time-alerts` (alerts.pb, 60s), `vehicle-positions` (positions.pb, 20s). `build-config.json`: `transitFeeds: [{type: gtfs, feedId: bogota, source: file:///var/opentripplanner/bogota-gtfs.zip}]`, `osm: [{source: file:///var/opentripplanner/bogota.osm.pbf}]`, `transitModelTimeZone: America/Bogota`. Memory: `JAVA_TOOL_OPTIONS=-Xmx10G` for build, `-Xmx6G` serve.
- API translates `plan` → OTP `planConnection` GraphQL and normalizes to the schema above. Never expose raw OTP responses.

## Web app (opentransit-web) scope v1
Routes: `/` (city picker → redirect if only one) · `/{city}` (map + planner) · `/{city}/stops/{stopId}` · `/{city}/routes/{routeId}` · `/{city}/live` (vehicles) · `/{city}/alerts` · `/about`.
Planner: origin/destination inputs with geocode autocomplete + "use my location" + click-on-map; depart/arrive time; mode toggles; wheelchair toggle; itinerary cards (time, duration, transfers, walk, mode chips colored by route); itinerary detail with legs + steps; map draws legs, stops, live vehicles on the itinerary's routes. Share URL with query params. PWA manifest. i18n es/en. Dark mode.

## Mobile app (opentransit-mobile) scope v1
Screens: city picker (first launch, remembered) · Home map (nearby stops, live vehicles toggle, search bar) · Plan trip sheet · Results list · Itinerary detail (map + timeline) · Stop detail (departures, auto-refresh 20s) · Route detail · Alerts · Favorites (stops, routes, places; local storage) · Settings (city, language, accessibility, theme). Location via geolocator. Deep links `opentransit://{city}/plan?...`.

## v1.1 additions (implemented 2026-09-04)

Everything below is live in this repo and covered by tests. Ideas come from the TransMi App / Maas analysis
(`docs/ROADMAP-v1.1.md`). Nothing here needs data the city does not publish.

### City (extended)
```jsonc
City {
  ...v1 fields...,
  "components": [ {"id": "trunk", "label": "Troncal", "color": "#D32F2F", "icon": "brt"}, ... ],   // icon: brt|bus|cable|rail|tram|ferry
  "fares": {"currency": "COP", "base": 3200, "transfer": 0, "transferWindowMinutes": 110, "maxTransfers": 2,
            "note": "Valor estimado…", "estimated": true} | null,
  "config": {"vehiclePollSeconds": 15, "departuresRefreshSeconds": 20,
             "features": {"liveVehicles": true, "board": true, "pois": true, "followAlong": true, "bike": true},
             "minAppVersion": {"ios": "1.0.0", "android": "1.0.0"}, "maintenance": {"active": false, "message": null}},
  "links": {"pqrs": "https://…", "recharge": "https://…", "support": "https://…", "privacy": "https://…"},
  "services": [ {"id": "recharge", "label": "Recargar tullave", "icon": "card", "url": "https://…", "kind": "external"} ]
}
```
`components` is derived from `agencies` when the YAML does not declare it. `features.fares` (GTFS fares) stays
`false` for Bogotá; `fares` is the flat-fare **estimate** config.

### Itinerary.fare (was always null)
`{"amount": 3200, "currency": "COP", "estimated": true, "breakdown": [{"label": "Pasaje", "amount": 3200, "route": "G12"}, {"label": "Transbordo", "amount": 0, "route": "TC14"}]}`.
Rule: first boarding pays `base`; later boardings within `transferWindowMinutes` of that boarding pay `transfer`
(at most `maxTransfers` of them); anything else pays `base` again and restarts the window. Walk-only itineraries
get `amount: 0`. `null` only when the city has no `fares`. Labels follow `locale` (es/en).

### RouteRef.serviceWindow
```jsonc
"serviceWindow": {"start": "04:30", "end": "23:53", "endsNextDay": false, "active": true,
                  "nextStart": null | "04:30", "nextStartDay": null | "today" | "tomorrow",
                  "hasServiceToday": true, "source": "gtfs"}
```
Computed at ingest per route × service_id from `stop_times` (first/last departure) widened by `frequencies.txt`,
resolved at request time against `calendar` + `calendar_dates` in the city timezone. Windows that cross midnight
stay `active` after 00:00 (`endsNextDay: true`; `end` is shown mod 24 h). Present on `/routes`, `/routes/{id}`,
`Departure.route`, board rows, next-bus route and plan legs; `null` for routes unknown to the static feed.

### Board and next buses
- `GET /v1/cities/{city}/stops/{stopId}/board?minutes=60&perRoute=3`
  `{ "stop": Stop, "generatedAt", "freshness": {"realtime": true, "ageSeconds": 18, "staleSeconds": 4, "stale": false},
     "rows": [ {"route": RouteRef, "headsign": string|null, "next": [ {"time", "minutes", "realtime", "delaySeconds", "tripId", "vehicleId"} ]} ] }`
  Rows are grouped by (route, headsign), sorted by the first `minutes`; stations aggregate their platforms.
  Routes that serve the stop but have nothing in the window are appended with `next: []` so the client can show
  "Fuera de horario · próximo HH:MM" from `route.serviceWindow`.
- `GET /v1/cities/{city}/stops/{stopId}/routes/{routeId}/next?limit=3&minutes=90`
  `{ "stop", "route", "generatedAt", "freshness", "servesStop": true, "vehiclesOnRoute": 13,
     "next": [ {"minutes": 4, "time", "source": "live"|"estimated"|"scheduled", "vehicle": Vehicle|null,
                "stopsAway": 2|null, "distanceMeters": 2442|null, "tripId", "delaySeconds"} ] }`
  `live`/`estimated` rows come from vehicles of that route located upstream of the stop on one of its patterns
  (by the RT `stop_id`, else by projecting the position onto the pattern geometry, ≤ 250 m off-line).
  ETA is OTP's realtime departure for that trip (`live`) or distance ÷ component speed + 20 s dwell per stop
  (`estimated`). Remaining slots are filled with scheduled departures (`scheduled`). `servesStop: false` when no
  pattern of the route calls at the stop (then `next` is empty).

### Health, alerts
- `health.realtime.stale` (true when the last fetch failed, is older than 90 s, or the p50 entity age > 90 s) and
  `health.realtime.staleSeconds` (seconds since the last successful fetch). The same block is `freshness` on board/next.
- `Alert.severity` is always one of `INFO|WARNING|SEVERE`; `severitySource: "feed"|"inferred"` says whether it came
  from the feed or was derived from `effect` (NO_SERVICE/REDUCED_SERVICE/SIGNIFICANT_DELAYS → SEVERE;
  DETOUR/MODIFIED_SERVICE/STOP_MOVED/ACCESSIBILITY_ISSUE → WARNING; else INFO).

### POIs (station services layer)
`GET /v1/cities/{city}/pois?bbox=minLon,minLat,maxLon,maxLat&type=bike_parking,toilets&limit=2000` → GeoJSON
FeatureCollection; properties `{id, type, name?, source: "osm", osmId, wheelchair?, operator?, openingHours?, capacity?, fee?, covered?}`;
`meta: {count, total, types}`. Types: `bike_parking, toilets, atm, health, library, police, pharmacy`.
Data file `cities/<slug>/pois.geojson` (Bogotá: 3,288 features) is built by `scripts/build-pois.sh <slug>`
(Overpass API, bbox from the YAML, one retry) and committed; override the path with `pois_file` in the YAML.

### Stop.accessibility
`{"wheelchair": "accessible"|"not_accessible"|"unknown", "source": "gtfs"|"osm"|"none", "verified": false, "note": "Dato del feed no verificado…"|null}`.
`verified` is `false` when the ingest finds one informative `wheelchair_boarding` value on ≥ 99 % of stops
(Bogotá: `1` on 8,335/8,335). The legacy `wheelchair` field is kept. `source: "osm"` is reserved (not produced yet).

### Vehicles stream filter
`GET /vehicles/stream?bbox=…&routeIds=a,b` filters the first frame and every delta server-side; a vehicle that
leaves the filter is reported in `removed` so clients keep a plain id-keyed map. `count` is the number of
vehicles currently inside the filter for that connection.

### Geocode ranking
With `lat/lon`, GTFS stops/stations within 800 m come first (closest first, `distanceMeters` filled), then
stations, then other stop matches, then Photon. Without a position: stations first (as before).


## v1.2 additions (implemented 2026-09-04) — shared bikes (GBFS)

Shared-vehicle networks are **per-city configuration** (`cities/<city>.yaml` → `mobility.bike_share[]`, also
editable at runtime through the admin config under `mobility`). Nothing in the code names a provider; a city
may declare several networks (each with its own id, colour, GBFS url and OTP updater network id).

### City (extended)
```jsonc
"features": { ..., "bikeShare": true },            // true when at least one network is configured
"mobility": { "bikeShare": [ { "id": "<slug>", "name": "...", "network": "<otp updater network id>",
  "gbfsUrl": "https://.../gbfs.json", "color": "#RRGGBB", "url": "https://...",
  "apps": {"ios": "https://...|null", "android": "https://...|null"},
  "pricingSummary": "string|null",   // from system_pricing_plans when null in the config
  "singleTripPrice": {"amount": 4850, "currency": "COP", "label": "1 viaje"} | null,   // pins the price estimate; else GBFS heuristic
  "formFactors": ["bicycle", "scooter"] } ] }
```

### Plan
- `modes` accepts `BIKE_RENTAL` (alias `BICYCLE_RENTAL`) and `SCOOTER_RENTAL`. With transit they become OTP
  **access/egress** modes (next to WALK) and a **direct** alternative; without transit, direct only.
  `400 MODE_UNAVAILABLE` when the city has no network configured.
- Rental legs keep `mode: "BICYCLE"|"SCOOTER"`, `transit: false`, and carry:
```jsonc
"rental": { "networkId": "<slug>", "networkName": "...", "color": "#RRGGBB",
  "vehicleType": "bicycle"|"electric_assist"|"scooter"|null,
  "pickup":  { "stationId": "<slug>:<gbfs station_id>", "name": "...", "lat": .., "lon": .., "vehiclesAvailable": 6, "docksAvailable": 13, "lastReported": "..." },
  "dropoff": { ...same shape... }, "freeFloating": false,
  "priceEstimate": { "amount": 11000, "currency": "COP", "label": "Diario", "estimated": true } | null }
```
  Availability comes from the API's own GBFS cache (fresher than OTP's copy); station ids are scoped with
  **our** network id, never OTP's.
- `Place.rentalStationId` on the walk legs that meet a rental station.
- `Itinerary.rentalLegs` (int) and `Itinerary.modesUsed` (e.g. `["WALK","BICYCLE_RENTAL","BUS"]`).
- `fare.breakdown[].kind` is `"transit"` or `"rental"`; one rental pass is charged per network per itinerary
  (a pass covers both the access and the egress ride). The pass is the cheapest *real* day/single-ride plan in
  `system_pricing_plans` (test, free/promo, subsidised and partner plans are skipped; the modal price of the
  single-ride plans wins, latest tariff on ties) unless the network config pins `singleTripPrice`. `fare` exists even when the city
  has no transit fares configured, as long as a rental leg is priced.

### Rental endpoints (served from memory; the API polls each GBFS feed honouring its `ttl`)
- `GET /v1/cities/{city}/rental/networks` → `{ "networks": [ { ...City.mobility.bikeShare[i], "systemId", "systemName", "timezone", "gbfsVersion", "stations", "vehiclesAvailable", "vehicleTypes": [{id, formFactor, propulsion, name}], "pricingPlans": [{id, name, price, currency, description, isTaxable}], "lastFetchAt", "up", "error" } ] }` (cache 5 min)
- `GET /v1/cities/{city}/rental/stations?bbox=&networkId=&limit=500` → `{ "generatedAt", "ttlSeconds", "stations": [ { "id": "<slug>:<id>", "networkId", "kind": "rental_station", "name", "lat", "lon", "capacity", "vehiclesAvailable", "ebikesAvailable", "docksAvailable", "isInstalled", "isRenting", "isReturning", "lastReported" } ] }`
- `GET /v1/cities/{city}/rental/stations/{id}` → station + `"vehicleTypesAvailable": [{id, formFactor, propulsion, name, count}]` + `"network": {...}`; `404 RENTAL_STATION_NOT_FOUND`.
- `GET /v1/cities/{city}/stops/nearby?include=stops,rental` → adds `"rentalStations": [ station & {"distanceMeters"} ]`
  (default `include=stops`; rental stations are returned in their own array, not mixed into `stops`).
- `GET /v1/cities/{city}/health` gains `"rental": { "networks": [ {"id", "up", "stations", "vehiclesAvailable", "ageSeconds", "error"} ] }`.

### Admin
`PUT /v1/admin/cities/{city}/config` accepts `"mobility": { "bikeShare": [...] }` (validated: slug id, https
`gbfsUrl`/`url`/apps, hex colour, unique ids). Saving re-syncs the in-memory GBFS pollers; the OTP updater
still needs `scripts/otp-updaters.py <city>` + an OTP restart (documented in the README).

### OTP
`scripts/otp-updaters.py <city>` generates one `vehicle-rental` (GBFS) updater per configured network into
`otp/<city>/router-config.json` (`network` = YAML `network`, url = `gbfs_url`); `scripts/otp-native.sh serve`
runs it automatically. `--check` fails when the file is stale (used by CI).

## Implementation notes & deviations (what this repo actually does)

- **v1.2 rental (deviations from CONTRACT-bikeshare.md):** `stops/nearby` returns rental stations in a separate `rentalStations` array (typed) instead of mixing them into `stops`; `priceEstimate` is one pass per network per itinerary; `Itinerary.fare` is non-null for rental-only trips even without city fares; OTP 2.9 counts (`availableVehicles.total`) are flattened; a rental leg from a network the city does not configure is still returned with `networkId` = OTP's id and no colour/price.

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
| `serviceWindow.end` / `nextStart` format | "HH:MM" | "HH:MM" local, hours mod 24; `endsNextDay: true` marks windows that end after midnight. Extra fields `nextStartDay`, `hasServiceToday`. |
| `next` `source` | `live`/`scheduled` | adds `estimated` (vehicle upstream but no realtime time for its trip: distance-based ETA). Extra `servesStop`, `vehiclesOnRoute`. |
| `Alert.severity` | may be null | never null; `severitySource` tells feed vs inferred. |
| `Stop.accessibility.source: "osm"` | OSM `wheelchair=*` | not produced yet (only `gtfs`/`none`); OSM accessibility lives in the POI layer (`properties.wheelchair`). |
| `GeocodeResult.distanceMeters` | – | added when `lat/lon` are given. |
