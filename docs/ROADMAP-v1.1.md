# opentransit v1.1 — feature plan ("lo mejor de TransMi App y Maas")

Source of each idea: **T** = TransMi App 2.9.7, **M** = Maas by Vettica 11.9.3, **T+M** = both. Evidence in `REFERENCE-APPS.md`.
Every feature works with open data only (GTFS + GTFS-RT + OSM). Partner-dependent modules (card top-up, TransMiPass, parking, taxi) are hand-off tiles, never core.

| # | Feature | Src | api | web | mobile |
|---|---|---|---|---|---|
| 1 | Question-led home hub: tiles Planear viaje · Ubica tu bus · Paradas cerca · Buscar ruta · Buses en vivo · Alertas · Favoritos, plus "Estaciones y paradas cerca" map card | T+M | – | ✅ | ✅ |
| 2 | "Ubica tu bus": station → route → next buses, each row labeled **En vivo** / **Por programación** | T | `/stops/{id}/routes/{routeId}/next` | ✅ | ✅ |
| 3 | Arrival board at a stop: grouped by route, "Siguiente en 5 min · luego 10, 15 y 20", live/scheduled badge per time | M | `/stops/{id}/board` | ✅ | ✅ |
| 4 | Scheduled fallback + freshness: "Programado" / "En vivo" / "Sin datos en vivo hace N s" (from `health.realtime.entityAgeP50Seconds`) | T | `health.realtime.stale` | ✅ | ✅ |
| 5 | Live markers: colored by component, sized/tinted by ETA bucket (≤5/≤10/≤15 min) to the selected stop, and **interpolated** between SSE frames | T+M | `bearing` already; `vehicles?bbox=` on stream | ✅ | ✅ |
| 6 | Service hours per route: `serviceWindow` (first/last departure today, active now) → "Fuera de horario · próximo 04:30" | T | ingest + `RouteRef.serviceWindow` | ✅ | ✅ |
| 7 | Estimated fare per itinerary from city config (base, transfer cost, transfer window, max transfers) shown as **"Tarifa estimada"** | M | `city.fares` + `Itinerary.fare{amount,currency,estimated:true,breakdown}` | ✅ | ✅ |
| 8 | Result sorting chips: Más rápido · Menos transbordos · Menos caminata · Más económico · Salida más próxima | M | – | ✅ | ✅ |
| 9 | Component taxonomy: distinct icon + color per component (trunk, feeder, dual, zonal, cable) everywhere | T | `city.components[]` (label, color, icon) | ✅ | ✅ |
| 10 | Typed favorites (Casa, Trabajo, custom) for places, stops and routes, with live context on the favorites screen (next departures, service window); recent trips (last 10) for one-tap replan | T+M | – (local-first) | ✅ | ✅ |
| 11 | Alert carousel on home: severity-sorted, dismissible, capped impressions per alert id, link to `url` | T | `severity` inference | ✅ | ✅ |
| 12 | Remote config per city: `config.vehiclePollSeconds`, feature flags, `minAppVersion{ios,android}`, `maintenance{active,message}`; `links{pqrs,recharge,support}`; `services[]` hand-off tiles | M | `/v1/cities/{city}` | ✅ | ✅ (forced-update + maintenance screens) |
| 13 | Deep links + station QR: canonical `https://<web>/{city}/stops/{id}` etc., mobile claims them (App Links / Universal Links); web QR generator on stop and route pages | T+M | – | ✅ | ✅ |
| 14 | "Iniciar viaje" follow-along: current leg highlighted, "próxima parada es la tuya" local notification from device location vs leg geometry | T | – | ⚠️ simple (highlight + progress) | ✅ |
| 15 | Station services layer (POIs): bike parking, toilets, ATMs, health points, libraries from OSM, as a toggleable map layer | T | `/pois?bbox=&type=` from per-city GeoJSON + `scripts/build-pois.sh` (Overpass) | ✅ | ✅ |
| 16 | Honest accessibility: `Stop.accessibility{wheelchair, source, verified}`; when the feed's value is constant across all stops it is flagged **"no verificado"**; OSM `wheelchair=*` used when present | T | ingest heuristic + model | ✅ | ✅ |
| 17 | Nearby-first search: geocoder results lead with nearby stops when `lat/lon` given; routes per stop as component chips | T | ranking tweak | ✅ | ✅ |
| 18 | Bike-to-station option: "Llegar en bici a la estación" (BICYCLE + TRANSIT) with bike parking POIs at portals | T | exists | ✅ | ✅ |
| 19 | PQRS / report hand-off: link to the agency's official channels from alerts, stop and vehicle screens | T | `city.links.pqrs` | ✅ | ✅ |

Not adopted (deliberately): account/location gates before planning, ad/marketing SDKs, phone permissions, WebView-embedded core features, closed planner proxies, NFC card top-up (needs SAM licence + PCI), Reporteador with identity + photo.

## API contract additions (v1.1) — clients build against this

### City (extended)
```jsonc
City {
  ...v1 fields...,
  "components": [ {"id": "trunk", "label": "Troncal", "color": "#D32F2F", "icon": "brt"}, {"id": "feeder", "label": "Alimentador", "color": "#2E7D32", "icon": "bus"},
                  {"id": "dual", "label": "Dual", "color": "#6A1B9A", "icon": "bus"}, {"id": "zonal", "label": "Zonal", "color": "#1565C0", "icon": "bus"}, {"id": "cable", "label": "TransMiCable", "color": "#EF6C00", "icon": "cable"} ],
  "fares": {"currency": "COP", "base": 3200, "transfer": 0, "transferWindowMinutes": 110, "maxTransfers": 2, "note": "Valores configurables; verificar con tarifa vigente", "estimated": true},
  "config": {"vehiclePollSeconds": 15, "departuresRefreshSeconds": 20, "features": {"liveVehicles": true, "board": true, "pois": true, "followAlong": true, "bike": true},
             "minAppVersion": {"ios": "1.0.0", "android": "1.0.0"}, "maintenance": {"active": false, "message": null}},
  "links": {"pqrs": "https://...", "recharge": "https://...", "support": "https://...", "privacy": null},
  "services": [ {"id": "recharge", "label": "Recargar tullave", "icon": "card", "url": "https://...", "kind": "external"} ]
}
```
### Itinerary.fare (was always null)
`"fare": {"amount": 3200, "currency": "COP", "estimated": true, "breakdown": [{"label": "Pasaje", "amount": 3200}, {"label": "Transbordo", "amount": 0}]}` — computed: transit legs → 1 base + (transfers within window ≤ maxTransfers ? transfer : base each). `null` only when the city has no `fares`.

### RouteRef.serviceWindow
`"serviceWindow": {"start": "04:00", "end": "23:00", "active": true, "nextStart": null | "04:00", "source": "gtfs"}` (today's service per calendar/calendar_dates; computed at ingest per route × service_id). Present on `/routes`, `/routes/{id}` and `Departure.route`; may be `null` on plan legs.

### Board and next buses
- `GET /v1/cities/{city}/stops/{stopId}/board?minutes=60&perRoute=3` →
  `{ "stop": Stop, "generatedAt": "...", "freshness": {"realtime": true, "ageSeconds": 18, "stale": false}, "rows": [ {"route": RouteRef, "headsign": "...", "next": [ {"time": "...", "minutes": 5, "realtime": true, "delaySeconds": -60, "tripId": "...", "vehicleId": "..."|null} ] } ] }` (rows sorted by first `minutes`; stations aggregate children).
- `GET /v1/cities/{city}/stops/{stopId}/routes/{routeId}/next?limit=3` →
  `{ "stop": Stop, "route": RouteRef, "freshness": {...}, "next": [ {"minutes": 4, "time": "...", "source": "live"|"scheduled", "vehicle": Vehicle|null, "stopsAway": 3|null, "distanceMeters": 1200|null, "tripId": "..."} ] }` — `live` rows come from vehicles on that route whose `stopSequence` is upstream of this stop in the pattern (ETA = OTP realtime time if present else distance/avg speed heuristic labeled `"source":"estimated"`); `scheduled` rows fill the rest from departures.

### Health
`health.realtime.stale: bool` (true when `entityAgeP50Seconds > 90` or last fetch failed) and `health.realtime.staleSeconds`.

### Alerts
`severity` inferred when the feed omits it: `SEVERE` if effect in {NO_SERVICE, REDUCED_SERVICE, SIGNIFICANT_DELAYS}, `WARNING` for DETOUR/MODIFIED_SERVICE/STOP_MOVED, else `INFO`.

### POIs
`GET /v1/cities/{city}/pois?bbox=minLon,minLat,maxLon,maxLat&type=bike_parking,toilets,atm,health,library` → GeoJSON FeatureCollection, properties `{id, type, name, source: "osm", osmId, wheelchair}`. Data file `cities/{city}/pois.geojson` built by `scripts/build-pois.sh <city>` (Overpass API, bbox from city.yaml). Ship a real Bogotá file.

### Stop.accessibility
`"accessibility": {"wheelchair": "accessible"|"not_accessible"|"unknown", "source": "gtfs"|"osm"|"none", "verified": false, "note": "Dato del feed no verificado"}`. `verified=false` when the ingest detects a constant `wheelchair_boarding` across ≥ 99 % of stops. Keep the old `wheelchair` field for compatibility.

### Vehicles stream
`GET /vehicles/stream?bbox=...&routeIds=a,b` filters both the full frame and deltas server-side (removed ids still sent).

### Geocode ranking
When `lat/lon` given: stops/stations within 800 m rank first (distance-weighted), then name matches, then Photon.
