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
