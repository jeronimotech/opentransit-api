# Changelog

All notable changes to `opentransit-api` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses `main` as the release line until tagged
releases start.

## [Unreleased]

### Added
- **Sign in with Google or Microsoft, beside the password.** OpenID Connect authorization code flow with
  PKCE (S256), configured per deployment and **off unless configured** — the login screen shows a button
  only for a provider whose client id *and* secret are set, and an unconfigured provider's endpoints answer
  `404`. New endpoints: `GET /v1/admin/auth/providers` (public, names only),
  `POST /v1/admin/auth/oidc/{provider}/start`, `POST /v1/admin/auth/oidc/{provider}/callback`.
  - **It never creates an account.** A verified provider email signs in an existing, **enabled**
    `admin_user`; anybody else gets `403` and is told to ask an owner for an invitation. Delegating "who is
    an operator here" to "who can prove they own an address" is the failure mode this rules out.
  - **The account is bound to the provider's stable subject, not to the email string.** New `admin_identity`
    table (Google `sub`, Microsoft `oid`): the first sign-in links it, later sign-ins must present the same
    subject for that account *and* the same account for that subject. Either mismatch is refused rather than
    re-linked, so a recycled address never inherits an admin account.
  - **The id_token is verified**: RS256 signature against the provider's JWKS (`alg: none` and symmetric
    algorithms refused before a key is fetched, unknown `kid` refetches the key set once), issuer, audience,
    `exp`/`iat`/`nbf` with 60 s leeway, and the `nonce` from the request that started the flow.
  - **Email verification per each provider's own rules**: Google must report `email_verified: true`;
    Microsoft is restricted to an explicit tenant-id allowlist and refuses a token whose `xms_edov` is
    present and false. Microsoft sign-in will not enable without that allowlist (`MICROSOFT_TENANT` as a
    GUID, or `MICROSOFT_ALLOWED_TENANT_IDS`) because Entra signs every tenant with the same keys.
  - `state` and the PKCE verifier are single-use, expire in `OIDC_STATE_TTL_SECONDS` (default 10 min) and
    are bound to the browser that started the flow; new `admin_oidc_state` table, consumed with a
    `DELETE … RETURNING`, swept by the maintenance loop. The `redirect_uri` is computed from
    `OIDC_REDIRECT_BASE` and never accepted from the caller.
  - The session it issues is the **same `admin_session`** the password flow issues: same roles, same city
    scope, same expiry, same revocation, same httpOnly-cookie proxy.
  - New settings: `OIDC_REDIRECT_BASE`, `OIDC_STATE_TTL_SECONDS`, `OIDC_ALLOW_INSECURE_HTTP`,
    `GOOGLE_CLIENT_ID`/`_SECRET`, `MICROSOFT_CLIENT_ID`/`_SECRET`/`MICROSOFT_TENANT`/
    `MICROSOFT_ALLOWED_TENANT_IDS`. Redirect URIs to register are in `docs/DEPLOY-RAILWAY.md` §4c.
  - Optional and **off by default**: `OIDC_AUTO_PROVISION_DOMAINS` creates an account for a verified address
    at a listed domain. It is documented in `SECURITY.md` as the risk it is and logs a warning at start-up.
- `scripts/admin_user.py unlink <email> --provider google|microsoft` — the deliberate recovery when a linked
  identity legitimately changes; `list` now shows which providers each account is linked to.
- `PyJWT[crypto]` is a new runtime dependency (id_token signature and claim validation).

### Added
- **Named admin accounts replace the shared `ADMIN_TOKEN` for people.** An `admin_user` table (email unique
  case-insensitively, argon2id password digest, name, role, city scope, disabled, timestamps) and an
  `admin_session` table that stores only the sha256 of the session token, its expiry, and the user agent that
  opened it. New endpoints: `POST /v1/admin/auth/login` · `…/logout` · `GET /v1/admin/auth/me`, and
  `GET/POST /v1/admin/users`, `PATCH …/users/{id}`, `POST …/users/{id}/disable` for owners.
  - Roles `viewer` < `admin` < `owner`, and a city scope (`[]` = every city). **Both are enforced on every
    admin route**, including the analytics, assistant-health and open-mobility ones — a role that is too low
    or a city outside the scope now gets `403 FORBIDDEN` rather than being a UI-only distinction.
  - Sessions expire (`ADMIN_SESSION_HOURS`, default 12) and are revoked on sign-out, on disable, and whenever
    a password, role or city scope changes. Expired rows are swept by the maintenance loop. The last enabled
    owner cannot be disabled or demoted.
  - Repeated failed sign-ins are throttled per email and per client address; a wrong email, a wrong password
    and a disabled account are indistinguishable in the response, and a missing account still pays for one
    hash so timing does not leak either.
  - The config audit trail now records the signed-in account: `updatedBy` sent by a person is ignored.
- **`scripts/admin_user.py`** — `create-owner` (refuses once any account exists), `create`, `passwd`,
  `disable`, `enable`, `list`. Passwords are prompted, never passed as a flag. `ADMIN_BOOTSTRAP_EMAIL` +
  `ADMIN_BOOTSTRAP_PASSWORD` do the same at start-up for platforms without a shell, with the same refusal.
- `argon2-cffi` is a new runtime dependency.

### Changed
- **`ADMIN_TOKEN` is now an explicitly-labelled machine credential.** `X-Admin-Token` keeps working exactly as
  before for CI, cron and `make ingest` — it holds the `admin` role over every city — but it can never manage
  accounts, every use is logged, and `ADMIN_TOKEN_ENABLED=false` switches it off. Nothing automated breaks.
- `GET /v1/admin/me` gained `user` and `canManageUsers` beside the existing `ok` and `cities`; it is an alias
  of `GET /v1/admin/auth/me`.
- New error code `FORBIDDEN` (403) for "authenticated, but not allowed".

### Added
- Vehicle `bearing` is now **derived server-side** from consecutive positions when the feed omits it, with a
  new `bearingSource` (`feed` | `derived` | `null`) so clients can be honest about where it came from and
  automatically prefer the feed if an agency starts publishing it. Bogotá's GTFS-RT publishes no bearing on
  any vehicle, so direction arrows and marker tips had never been drawn there. Jitter under 10 m is ignored,
  pairs more than 180 s apart are refused, the value is smoothed across two frames, and it is never
  defaulted to 0. Exposed in `/vehicles`, the SSE stream (deltas included) and `/vehicles/{id}`.

## [2.0.0] - 2026-09-07 — conversational assistant (phase 1: text)

### Added
- **`POST /v1/cities/{city}/chat`** (SSE): a conversational assistant that may not answer a transit question
  from its own knowledge. It calls ten tools that run this API's own code paths in-process — `plan_trip`,
  `find_place`, `next_departures`, `locate_bus`, `service_alerts`, `fare_estimate`, `nearby_stops`,
  `bike_stations`, `vehicles_near`, `route_info` — and paraphrases what they return, so a hallucinated
  departure time is structurally impossible. Events: `token`, `tool`, `card`, `done`, `error`; each `card` is
  emitted the moment its tool returns, before the prose about it, so the app paints the itinerary first and
  the sentence second.
- Three provider adapters behind one neutral event stream, so clients never learn which provider a city
  configured: Anthropic (`anthropic` SDK, `messages.stream`, `strict: true` tools, `effort: "low"`,
  `cache_control` on the tools→system prefix), OpenAI-compatible (covers DeepSeek at its own `baseUrl`) and
  Gemini (Interactions API). Parallel tool calls run concurrently and all their results return in a single
  user message. Model ids and prices verified against each provider's own documentation on 2026-09-07.
- **`config.assistant`**, admin-editable: provider, model, key, limits, budget and `systemExtra`. The API key
  is masked on read and an omitted key keeps the stored one, exactly like the on-demand credentials; a masked
  key that matches the YAML value is dropped rather than copied, so a key held in the environment never gets
  written into the database.
- **`GET /v1/cities/{city}/chat/health`** (admin): provider, model, today's spend, calls and errors.
- Per-city daily USD budget metered from the provider's reported token usage, refused hard with
  `ASSISTANT_BUDGET` (503) when exhausted; per-session rate limit (`ASSISTANT_RATE_LIMITED`, 429) and reply
  cap; `ASSISTANT_DISABLED` (404) where the assistant is off.
- `assistant_query` analytics event carrying `{toolsUsed, latencyMs, ok}` and nothing else — the schema drops
  free text and coordinates, so a question or an answer cannot be recorded even by accident.

### Fixed
- An admin config PUT no longer resets `config.share` and `config.push` to their defaults: both are now
  carried through the effective-city rebuild alongside the new assistant section.

## [1.7.0] - 2026-09-06 — "cuándo salir", shared ETA, wearables, Live Activities
### Added
- `GET /plan/forecast`: departure options across a window with gaps, service notes and a recommendation.
  Bounded to 8 upstream plans per request and cached 60 s, so it never degrades `/plan`.
- Shared ETA (`POST|GET|PATCH|DELETE /share/eta`): unguessable token, hashed write key, coarse positions,
  TTL clamped per city, rows dropped at expiry. No analytics linkage.
- `GET /watch/summary`: compact payload for a watch face (~1.4 KB for three stops, 15 s cache).
- `POST /live-activity/register|end`: config-gated; with `config.push.enabled` false the app drives its own
  Live Activity and no APNs key is needed.
- City config gains `share` and `push` (both admin-editable; APNs credentials stay in the environment).

### Fixed
- `GET /watch/summary` dropped any requested stop that had no upcoming departures, so a watch asking for two
  favourites could get one item back and appear to have lost the other. Requested stops are now always
  returned (empty `routes` when nothing is coming), `limit` bounds only the nearby fill, `perRoute` controls
  the times per route, and a failing or unknown stop no longer affects the rest of the payload.

## [1.6.0] - 2026-09-06 — Open Mobility Foundation (CDS 1.1.0 curbs, MDS 2.1.0 policy/geography), phase A
### Added
- Per-city `openMobility` config (admin-editable): CDS curbs (local inventory or mirrored URL, publish toggle),
  MDS policy/geography (authority URL, publish toggle, providers with masked credentials) and the park-and-ride
  block phase B will use. `features.openMobility` is derived.
- Public normalised endpoints `GET /v1/cities/{city}/curbs`, `/curbs/nearby`, `/zones`: legality evaluated in the
  city timezone with its holiday calendar, CDS priority resolution (lowest wins), `whyLegal`, `nextChange`,
  `priceLabel`, live availability fields, and `userClass` filtering with synonyms (`car`, `rideshare`, `delivery`…).
- Verbatim publishing endpoints `GET …/cds/curbs/zones|policies|areas` and `…/mds/policies|geographies` with the
  specs' envelopes, media types (`application/vnd.cds+json;version=1.1`, `application/vnd.mds+json;version=2.1`),
  `ETag`/`Last-Modified`, and 406 on an unsupported `Accept` version.
- Admin curb CRUD and MDS document import; both accept a spec document, a `{zones, policies}` pair or a GeoJSON
  FeatureCollection, deriving non-UUID ids deterministically so re-imports do not duplicate.
- `health.openMobility` block; `POST …/openmobility/refresh` and a background loop that mirrors configured feeds.
- `openMobility.cds.rateCurrency` / `rateMinorUnits`: CDS quotes an integer in the smallest denomination of the
  local currency and defines no currency field. Bogotá quotes whole COP (minor units 1), USD/EUR quote cents (100).
- Restricted-plane tables created but unused (`mds_vehicle`, `mds_status_change`, `mds_trip`, `cds_event`,
  `mds_provider_state`) so phase B can serve the MDS Agency API (operators push; JWT claims carry `provider_id`)
  and poll the Provider API into the same tables without a migration.

## [1.5.0] - 2026-09-06 — first-party analytics (usage + mobility), privacy by design
### Added
- `POST /v1/cities/{city}/events`: anonymous batch ingestion (≤ 50 events, gzip, schema-validated per type,
  unknown props dropped, free text never accepted, rate-limited in memory, `202 {accepted, rejected}`).
- Coordinates replaced by geohash-7 cells before storage; 5-minute time buckets; daily-rotating salted hashes for
  session/cohort ids; daily partitions with retention by `DROP TABLE`.
- Idempotent rollup (every 10 min) into hourly OD/place aggregates and daily route/stop/mode/search/provider/
  funnel/hours/platform aggregates, in the city time zone.
- Admin analytics: `summary` (with previous-period deltas), `od` (GeoJSON cells + pairs), `places`, `routes`,
  `stops`, `modes`, `searches`, `providers`, `funnel`, `hours`, `export.csv`, `rollup`; k-anonymity applied on
  every read and export.
- `config.analytics { enabled, retentionDays, kThreshold }` (admin-editable, public), `health.analytics`.
- `app/geohash.py` (dependency-free geohash), `ENABLE_ANALYTICS_JOBS`, `ANALYTICS_ROLLUP_SECONDS`.

## [1.4.0] - 2026-09-05 — on-demand mobility (taxi / ride-hailing), provider-agnostic
### Added
- `mobility.taxi_tariffs[]`, `mobility.on_demand[]` and `mobility.on_demand_policy` per city (admin-editable;
  provider credentials injected server-side, returned masked, stripped from public responses and history).
- Taximeter tariff engine (`app/tariff.py`): distance/waiting units, minimum fare, surcharges by night window,
  Sundays, public holidays (per country), tariff zones (polygons) and optional extras, ±band, es/en breakdown.
- `GET /ondemand/providers`, `GET /ondemand/estimate` (OTP direct car route + one quote per provider),
  `GET /ondemand/handoff` (deep link built server-side, `redirect=1` → 302, store/web fallback), `health.ondemand`.
- `onDemandPolicy.durationFactor` (default 1.4) / `nightDurationFactor` (1.1): car durations from OTP (free-flow)
  are scaled in estimates, on-demand plan legs (timeline kept consistent) and tariff waiting units.
- Admin PUT credential rules: omitted `credentials` keeps the stored secret, `null` clears, masked keeps.
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
