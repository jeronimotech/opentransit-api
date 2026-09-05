# Changelog

All notable changes to `opentransit-api` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses `main` as the release line until tagged
releases start.

## [Unreleased]

## [1.4.0] - 2026-09-05 — on-demand mobility (taxi / ride-hailing), provider-agnostic
### Added
- `mobility.taxi_tariffs[]`, `mobility.on_demand[]` and `mobility.on_demand_policy` per city (admin-editable;
  provider credentials injected server-side, returned masked, stripped from public responses and history).
- Taximeter tariff engine (`app/tariff.py`): distance/waiting units, minimum fare, surcharges by night window,
  Sundays, public holidays (per country), tariff zones (polygons) and optional extras, ±band, es/en breakdown.
- `GET /ondemand/providers`, `GET /ondemand/estimate` (OTP direct car route + one quote per provider),
  `GET /ondemand/handoff` (deep link built server-side, `redirect=1` → 302, store/web fallback), `health.ondemand`.
- `/plan?onDemand=true` (or mode `ONDEMAND`): a direct ride plus taxi-to-stop / stop-to-taxi combos
  (OTP `CAR_DROP_OFF` access / `CAR_PICKUP` egress) merged next to transit, `Leg.onDemand` with per-provider
  prices and hand-off links, `CAR_ONDEMAND` in `modesUsed`, `fare.breakdown[].kind = "ondemand"`, `fare.note`.
- Rental-aware planning: one OTP search per rental mode plus a rental-biased companion search, merged with a
  guarantee that the best two shared-bike options are returned when they exist (`Itinerary.source`).
### Changed
- `FareItem.amount` may be `null` (provider prices only shown in its app); `Itinerary.source` adds `ondemand`.
- Requested rental modes with no vehicles available are dropped with a `MODE_NO_VEHICLES` warning instead of
  producing an empty plan; `formFactors` lists `scooter` only when scooters are actually reported available.

## [1.3.0] - 2026-09-04 — white-label city landing
### Added
- `landing` city section (hero, apps, highlights, screenshots, stats, partners, open data, FAQ, contact, footer,
  SEO), admin-editable with strict validation, and `GET /v1/cities/{city}/landing` with live stats
  (routes, stops, vehicles live, bike stations, active alerts) cached 60 s.
- Nested environment defaults in city YAML (`${OTP_<CITY>_URL:-${OTP_URL:-…}}`).
- Open-source hygiene: security policy, issue/PR templates, Dependabot, CODEOWNERS, this changelog.
### Changed
- `docker-compose.yml` is parametrised by `CITY` (generic `otp` service, shared `OTP_URL`);
  `docker-compose.cities.yml` shows several cities on one host.
- `.env.example` no longer ships a usable admin token; generate one with `openssl rand -hex 32`.

## [1.2.0] - 2026-09-04 — shared bikes (GBFS)
### Added
- Provider-agnostic `mobility.bike_share[]` networks per city (GBFS 3.0/2.x client with per-feed TTL caching,
  localized names, e-bike counts, pricing heuristic), admin-editable.
- `GET /rental/networks`, `GET /rental/stations` (bbox), `GET /rental/stations/{id}`, `stops/nearby?include=rental`,
  `health.rental`.
- `BIKE_RENTAL` / `SCOOTER_RENTAL` planning modes (OTP access/egress/direct) with a `rental` block on legs
  (pickup/drop-off stations with live availability, price estimate), fare breakdown kinds, `rentalLegs`, `modesUsed`.
- `scripts/otp-updaters.py`: OTP `vehicle-rental` updaters generated from the city YAML (checked in CI).

## [1.1.1] - 2026-09-04 — runtime admin configuration
### Added
- Admin-editable city configuration (`fares`, `config`, `links`, `services`, `branding.primaryColor`) persisted in
  Postgres with a history table: `GET/PUT/DELETE /v1/admin/cities/{city}/config`, `…/config/history`,
  `GET /v1/admin/me`. Changes reach `/v1/cities/{city}` and fare estimates immediately.
### Changed
- `/network` serves canonical shapes only (exact duplicates and ≥ 90 %-covered variants dropped per route group),
  with `routeIds`, `directionId`, `lengthMeters`.

## [1.1.0] - 2026-09-04 — best of the reference apps
### Added
- Component palette, flat-fare estimate with breakdown, remote client config (poll intervals, feature flags,
  minimum app version, maintenance), official links, service hand-off tiles.
- Per-route service windows (`serviceWindow`) from stop_times + frequencies + calendars.
- `GET /stops/{id}/board` (arrival board grouped by route) and `GET /stops/{id}/routes/{routeId}/next`
  (next buses: live / estimated / scheduled).
- Station-services POI layer (`GET /pois`, Overpass builder script), accessibility heuristic
  (`Stop.accessibility`, flags constant feed values as unverified), realtime staleness in health, alert severity
  inference, vehicle stream `bbox`/`routeIds` filters, nearby-first geocoding.

## [1.0.0] - 2026-09-04 — first release
### Added
- Multi-city FastAPI service: city registry (`cities/*.yaml`), GTFS static ingest into PostGIS, GTFS-RT poller
  with SSE deltas, geocoding (GTFS stops + Photon), stops / departures / routes / network / vehicles / alerts /
  health endpoints, trip planning normalized from OpenTripPlanner 2 `planConnection`.
- Graph build script (GTFS + OSM clip), native and Docker OTP runners, Docker Compose stack, tests, CI.
