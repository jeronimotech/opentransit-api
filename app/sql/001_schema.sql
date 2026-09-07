-- opentransit-api schema. Everything is scoped by city so one database serves all tenants.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE TABLE IF NOT EXISTS feed_version (
  id            BIGSERIAL PRIMARY KEY,
  city          TEXT NOT NULL,
  fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_modified TIMESTAMPTZ,
  sha256        TEXT NOT NULL,
  feed_info     JSONB,
  n_routes INT, n_trips INT, n_stops INT, n_shapes INT,
  is_active     BOOLEAN NOT NULL DEFAULT FALSE,
  meta          JSONB,
  UNIQUE (city, sha256)
);
ALTER TABLE feed_version ADD COLUMN IF NOT EXISTS meta JSONB;
CREATE INDEX IF NOT EXISTS feed_version_active ON feed_version (city) WHERE is_active;

CREATE TABLE IF NOT EXISTS route (
  feed_version_id BIGINT NOT NULL REFERENCES feed_version(id) ON DELETE CASCADE,
  route_id    TEXT NOT NULL,
  agency_id   TEXT,
  short_name  TEXT,
  long_name   TEXT,
  route_type  INT,
  color       TEXT,
  text_color  TEXT,
  component   TEXT,
  PRIMARY KEY (feed_version_id, route_id)
);
CREATE INDEX IF NOT EXISTS route_short_trgm ON route USING gin (short_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS trip (
  feed_version_id BIGINT NOT NULL REFERENCES feed_version(id) ON DELETE CASCADE,
  trip_id   TEXT NOT NULL,
  route_id  TEXT,
  shape_id  TEXT,
  headsign  TEXT,
  direction_id SMALLINT,
  PRIMARY KEY (feed_version_id, trip_id)
);

CREATE TABLE IF NOT EXISTS stop (
  feed_version_id BIGINT NOT NULL REFERENCES feed_version(id) ON DELETE CASCADE,
  stop_id        TEXT NOT NULL,
  stop_code      TEXT,
  name           TEXT NOT NULL,
  name_norm      TEXT,               -- unaccented lower-case, for search
  lat            DOUBLE PRECISION NOT NULL,
  lon            DOUBLE PRECISION NOT NULL,
  geog           GEOGRAPHY(POINT, 4326) NOT NULL,
  location_type  SMALLINT NOT NULL DEFAULT 0,
  parent_station TEXT,
  wheelchair     SMALLINT NOT NULL DEFAULT 0,
  component      TEXT,               -- dominant component of the routes serving it
  n_routes       INT NOT NULL DEFAULT 0,
  PRIMARY KEY (feed_version_id, stop_id)
);
CREATE INDEX IF NOT EXISTS stop_geog ON stop USING gist (geog);
CREATE INDEX IF NOT EXISTS stop_name_trgm ON stop USING gin (name_norm gin_trgm_ops);

-- stop -> routes serving it (learned from stop_times.txt, streamed once, never stored)
CREATE TABLE IF NOT EXISTS stop_route (
  feed_version_id BIGINT NOT NULL REFERENCES feed_version(id) ON DELETE CASCADE,
  stop_id  TEXT NOT NULL,
  route_id TEXT NOT NULL,
  PRIMARY KEY (feed_version_id, stop_id, route_id)
);

CREATE TABLE IF NOT EXISTS shape_simplified (
  feed_version_id BIGINT NOT NULL REFERENCES feed_version(id) ON DELETE CASCADE,
  shape_id   TEXT NOT NULL,
  route_id   TEXT,
  component  TEXT,
  color      TEXT,
  n_points   INT,
  encoded    TEXT NOT NULL,
  direction_id SMALLINT,               -- from trips.txt when the feed has it
  is_canonical BOOLEAN NOT NULL DEFAULT TRUE,
  canonical_shape_id TEXT,             -- the shape that stands for this one when not canonical
  length_m   INT,
  group_key  TEXT,                     -- component + short name: the dedupe group
  represents TEXT[],                   -- route ids collapsed into this canonical shape
  PRIMARY KEY (feed_version_id, shape_id)
);
ALTER TABLE shape_simplified ADD COLUMN IF NOT EXISTS direction_id SMALLINT;
ALTER TABLE shape_simplified ADD COLUMN IF NOT EXISTS is_canonical BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE shape_simplified ADD COLUMN IF NOT EXISTS canonical_shape_id TEXT;
ALTER TABLE shape_simplified ADD COLUMN IF NOT EXISTS length_m INT;
ALTER TABLE shape_simplified ADD COLUMN IF NOT EXISTS group_key TEXT;
ALTER TABLE shape_simplified ADD COLUMN IF NOT EXISTS represents TEXT[];
CREATE INDEX IF NOT EXISTS shape_canonical ON shape_simplified (feed_version_id) WHERE is_canonical;

-- one row per minute per city: feed health series (small, kept forever)
CREATE TABLE IF NOT EXISTS feed_health (
  city              TEXT NOT NULL,
  minute            TIMESTAMPTZ NOT NULL,
  entity_age_p50_s  INT,
  n_vehicles        INT,
  n_trip_unresolved INT,
  pct_trip_resolved REAL,
  n_alerts          INT,
  fetch_ms          INT,
  http_status       INT,
  PRIMARY KEY (city, minute)
);

-- v1.1: per-route service windows (first/last departure per service_id) + calendars, for "Fuera de horario".
CREATE TABLE IF NOT EXISTS route_service_window (
  feed_version_id BIGINT NOT NULL REFERENCES feed_version(id) ON DELETE CASCADE,
  route_id    TEXT NOT NULL,
  service_id  TEXT NOT NULL,
  first_dep_s INT NOT NULL,          -- seconds since service-day midnight (may exceed 86400)
  last_dep_s  INT NOT NULL,
  PRIMARY KEY (feed_version_id, route_id, service_id)
);
CREATE TABLE IF NOT EXISTS service_calendar (
  feed_version_id BIGINT NOT NULL REFERENCES feed_version(id) ON DELETE CASCADE,
  service_id  TEXT NOT NULL,
  days        SMALLINT[] NOT NULL,   -- 7 flags, Monday first
  start_date  DATE NOT NULL,
  end_date    DATE NOT NULL,
  PRIMARY KEY (feed_version_id, service_id)
);
CREATE TABLE IF NOT EXISTS service_exception (
  feed_version_id BIGINT NOT NULL REFERENCES feed_version(id) ON DELETE CASCADE,
  service_id  TEXT NOT NULL,
  date        DATE NOT NULL,
  exception_type SMALLINT NOT NULL,  -- 1 added, 2 removed
  PRIMARY KEY (feed_version_id, service_id, date)
);

-- Admin-editable city configuration (fares, client config, links, services, primary colour).
-- The YAML stays the base; the override is deep-merged on top and served from memory.
CREATE TABLE IF NOT EXISTS city_config_override (
  city_id    TEXT PRIMARY KEY,
  data       JSONB NOT NULL,
  revision   INT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by TEXT
);
CREATE TABLE IF NOT EXISTS city_config_history (
  id         BIGSERIAL PRIMARY KEY,
  city_id    TEXT NOT NULL,
  revision   INT NOT NULL,
  data       JSONB NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  changed_by TEXT,
  note       TEXT
);
CREATE INDEX IF NOT EXISTS city_config_history_city ON city_config_history (city_id, revision DESC);

-- ═══════════════ v1.5 first-party analytics (privacy by design) ═══════════════
-- Raw events: no coordinates (geohash-7 cells only), 5-minute buckets, salted hashes, no IPs.
-- Partitioned by day of receipt so retention is a DROP TABLE, never a DELETE.
CREATE TABLE IF NOT EXISTS analytics_event (
  id           BIGSERIAL,
  city_id      TEXT NOT NULL,
  at_bucket    TIMESTAMPTZ NOT NULL,
  received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  type         TEXT NOT NULL,
  session_hash TEXT NOT NULL,
  cohort_hash  TEXT NOT NULL,
  platform     TEXT NOT NULL,
  app_version  TEXT,
  locale       TEXT,
  props        JSONB NOT NULL DEFAULT '{}'::jsonb,
  gh7          TEXT,
  from_gh7     TEXT,
  to_gh7       TEXT
) PARTITION BY RANGE (received_at);
CREATE INDEX IF NOT EXISTS analytics_event_city_recv ON analytics_event (city_id, received_at);
CREATE INDEX IF NOT EXISTS analytics_event_city_at   ON analytics_event (city_id, at_bucket);

CREATE TABLE IF NOT EXISTS analytics_salt (day DATE PRIMARY KEY, salt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analytics_rollup_state (
  city_id        TEXT PRIMARY KEY,
  watermark      TIMESTAMPTZ NOT NULL,
  last_rollup_at TIMESTAMPTZ
);

-- Aggregates (kept indefinitely; k-anonymity is applied on every read/export, never here).
CREATE TABLE IF NOT EXISTS agg_od_hourly (
  city_id TEXT NOT NULL, hour TIMESTAMPTZ NOT NULL, from_gh7 TEXT NOT NULL, to_gh7 TEXT NOT NULL, n INT NOT NULL,
  PRIMARY KEY (city_id, hour, from_gh7, to_gh7));
CREATE TABLE IF NOT EXISTS agg_place_hourly (
  city_id TEXT NOT NULL, hour TIMESTAMPTZ NOT NULL, gh7 TEXT NOT NULL, kind TEXT NOT NULL, n INT NOT NULL,
  PRIMARY KEY (city_id, hour, gh7, kind));
CREATE TABLE IF NOT EXISTS agg_route_daily (
  city_id TEXT NOT NULL, day DATE NOT NULL, route_id TEXT NOT NULL,
  views INT NOT NULL DEFAULT 0, selects INT NOT NULL DEFAULT 0, locates INT NOT NULL DEFAULT 0,
  PRIMARY KEY (city_id, day, route_id));
CREATE TABLE IF NOT EXISTS agg_stop_daily (
  city_id TEXT NOT NULL, day DATE NOT NULL, stop_id TEXT NOT NULL,
  views INT NOT NULL DEFAULT 0, boards INT NOT NULL DEFAULT 0, locates INT NOT NULL DEFAULT 0,
  PRIMARY KEY (city_id, day, stop_id));
CREATE TABLE IF NOT EXISTS agg_mode_daily (
  city_id TEXT NOT NULL, day DATE NOT NULL, mode_set TEXT NOT NULL,
  requests INT NOT NULL DEFAULT 0, selects INT NOT NULL DEFAULT 0,
  PRIMARY KEY (city_id, day, mode_set));
CREATE TABLE IF NOT EXISTS agg_search_daily (
  city_id TEXT NOT NULL, day DATE NOT NULL, result_type TEXT, result_id TEXT, label TEXT, n INT NOT NULL);
CREATE INDEX IF NOT EXISTS agg_search_daily_city ON agg_search_daily (city_id, day);
CREATE TABLE IF NOT EXISTS agg_provider_daily (
  city_id TEXT NOT NULL, day DATE NOT NULL, provider_id TEXT NOT NULL,
  handoffs INT NOT NULL DEFAULT 0, had_estimate INT NOT NULL DEFAULT 0,
  PRIMARY KEY (city_id, day, provider_id));
CREATE TABLE IF NOT EXISTS agg_funnel_daily (
  city_id TEXT NOT NULL, day DATE NOT NULL, app_opens INT NOT NULL DEFAULT 0, sessions INT NOT NULL DEFAULT 0,
  plan_requests INT NOT NULL DEFAULT 0, itinerary_selects INT NOT NULL DEFAULT 0,
  go_starts INT NOT NULL DEFAULT 0, go_completions INT NOT NULL DEFAULT 0,
  PRIMARY KEY (city_id, day));
CREATE TABLE IF NOT EXISTS agg_hours_daily (
  city_id TEXT NOT NULL, day DATE NOT NULL, hour SMALLINT NOT NULL, plan_requests INT NOT NULL DEFAULT 0,
  PRIMARY KEY (city_id, day, hour));
CREATE TABLE IF NOT EXISTS agg_platform_daily (
  city_id TEXT NOT NULL, day DATE NOT NULL, platform TEXT NOT NULL, app_version TEXT, n INT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS agg_platform_daily_city ON agg_platform_daily (city_id, day);

-- ═══════════════════════════════════════════════════════════════════
-- v1.6 · Open Mobility Foundation (phase A, OPEN data plane only).
-- CDS 1.1.0 curb zones/policies and MDS 2.1.0 policies/geographies are public regulation: they describe
-- the kerb and the rules, never a person or an operator's trips. The restricted plane (MDS Provider
-- trips/telemetry, CDS Events) is phase B and gets its own partitioned tables.
-- Documents are stored verbatim as JSONB so the publishing endpoints stay byte-faithful to the specs.
-- ═══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS curb_zone (
  city_id      TEXT NOT NULL,
  curb_zone_id TEXT NOT NULL,
  data         JSONB NOT NULL,
  min_lon      DOUBLE PRECISION, min_lat DOUBLE PRECISION,
  max_lon      DOUBLE PRECISION, max_lat DOUBLE PRECISION,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (city_id, curb_zone_id));
CREATE INDEX IF NOT EXISTS curb_zone_bbox ON curb_zone (city_id, min_lon, min_lat, max_lon, max_lat);

CREATE TABLE IF NOT EXISTS curb_policy (
  city_id        TEXT NOT NULL,
  curb_policy_id TEXT NOT NULL,
  data           JSONB NOT NULL,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (city_id, curb_policy_id));

CREATE TABLE IF NOT EXISTS mds_policy (
  city_id    TEXT NOT NULL,
  policy_id  TEXT NOT NULL,
  data       JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (city_id, policy_id));

CREATE TABLE IF NOT EXISTS mds_geography (
  city_id      TEXT NOT NULL,
  geography_id TEXT NOT NULL,
  data         JSONB NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (city_id, geography_id));

-- ── RESTRICTED plane (phase B; created now so the schema never has to be rewritten) ──────────────────
-- Written by BOTH MDS directions and never exposed publicly:
--   * Agency API server  — operators PUSH to us (JWT bearer whose claims carry `provider_id`)
--   * Provider API client — we POLL operators (OAuth2 client credentials, per-feed cursors)
-- Same tables either way, so metrics do not care where a record came from. Retention:
-- `openMobility.mds.retentionDays` (default 90), enforced by dropping day partitions.
CREATE TABLE IF NOT EXISTS mds_vehicle (
  city_id     TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  device_id   TEXT NOT NULL,
  vehicle_id  TEXT,
  mode        TEXT,
  vehicle_type TEXT,
  data        JSONB NOT NULL,
  last_event_at TIMESTAMPTZ,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  source      TEXT NOT NULL DEFAULT 'agency',   -- 'agency' (pushed) | 'provider' (polled)
  PRIMARY KEY (city_id, provider_id, device_id));

CREATE TABLE IF NOT EXISTS mds_status_change (
  city_id     TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  device_id   TEXT NOT NULL,
  event_at    TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  event_types TEXT[],
  vehicle_state TEXT,
  gh7         TEXT,                              -- coarsened, like analytics: never a raw point
  data        JSONB NOT NULL,
  source      TEXT NOT NULL DEFAULT 'agency'
) PARTITION BY RANGE (received_at);
CREATE INDEX IF NOT EXISTS mds_status_change_city ON mds_status_change (city_id, event_at);

CREATE TABLE IF NOT EXISTS mds_trip (
  city_id     TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  trip_id     TEXT NOT NULL,
  device_id   TEXT,
  start_at    TIMESTAMPTZ,
  end_at      TIMESTAMPTZ,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  distance_m  DOUBLE PRECISION,
  duration_s  INTEGER,
  start_gh7   TEXT,
  end_gh7     TEXT,
  data        JSONB NOT NULL,
  source      TEXT NOT NULL DEFAULT 'agency'
) PARTITION BY RANGE (received_at);
CREATE INDEX IF NOT EXISTS mds_trip_city ON mds_trip (city_id, start_at);

CREATE TABLE IF NOT EXISTS cds_event (
  city_id     TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  event_id    TEXT NOT NULL,
  curb_zone_id TEXT,
  event_at    TIMESTAMPTZ,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  event_type  TEXT,
  data        JSONB NOT NULL
) PARTITION BY RANGE (received_at);
CREATE INDEX IF NOT EXISTS cds_event_city ON cds_event (city_id, event_at);

CREATE TABLE IF NOT EXISTS mds_provider_state (
  city_id     TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  feed        TEXT NOT NULL,                     -- vehicles | status_changes | trips | events
  cursor      TEXT,
  last_poll_at TIMESTAMPTZ,
  last_error  TEXT,
  records     BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (city_id, provider_id, feed));

-- ─────────────── v1.7 shareable ETA links ───────────────
-- Deliberately unlinked to analytics: no session/cohort column exists here, and positions inside
-- `progress` are coarsened to 3 decimals by the API before the row is written. Rows die at `expires_at`.
CREATE TABLE IF NOT EXISTS share_eta (
  city_id    TEXT        NOT NULL,
  token      TEXT        NOT NULL,
  key_hash   TEXT        NOT NULL,          -- sha256 of the write key; the key itself is never stored
  itinerary  JSONB       NOT NULL,
  label      TEXT,
  started_at TEXT,
  progress   JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (city_id, token)
);
CREATE INDEX IF NOT EXISTS share_eta_expiry ON share_eta (expires_at);
