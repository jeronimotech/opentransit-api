# Bogotá — feed notes

Sources (official, TRANSMILENIO S.A.), all verified live on 2026-09-04:

| | URL | Notes |
|---|---|---|
| GTFS static | https://gtfs.transmilenio.gov.co/GTFS.zip | ~118 MB (570 MB unzipped). Published daily as `gs://gtfs-estaticos/GTFS_YYYYMMDD.zip` too; no `latest.zip` there. |
| Vehicle positions | https://gtfs.transmilenio.gov.co/positions.pb | ~6,000 vehicles, refresh ≈ 15 s, served as `text/plain` |
| Trip updates | https://gtfs.transmilenio.gov.co/tripupdates.pb | **one** `stop_time_update` per trip (next stop only) |
| Alerts | https://gtfs.transmilenio.gov.co/alerts.pb | ~300 active, all `UNKNOWN_CAUSE/DETOUR`, per route + stops |
| OSM | Geofabrik `south-america/colombia-latest.osm.pbf` clipped to `-74.45,3.95,-73.85,4.90` | 25 MB clipped |

Agencies (`agency_id` → component): 1 Troncal → `trunk`, 2 Alimentadores → `feeder`, 3 Dual → `dual`,
4/5/6 Zonal Urbano/Complementario/Especial → `zonal`, 7 TransMiCable → `cable`.

Known quirks and how the API handles them:

- **No fares** (`fare_attributes.txt` has a single TransMiCable row and no `fare_rules.txt`): `fare` is `null`.
- **No `transfers.txt` / `pathways.txt`**: transfers are geometric (OTP default); in-station routing is not modelled.
- **`wheelchair_boarding = 1` on all 8,335 stops**: a default, not a survey. The API passes it through as
  `accessible` but apps should not promise step-free access based on it.
- **Single year-long `calendar.txt`** — no seasonal/holiday variations. The graph is built with
  `transitServiceStart: -P1M`, `transitServiceEnd: P6M` to bound memory; rebuild the graph monthly (`make graph`).
- **~11 % of RT `trip_id`s don't exist in the static feed** (mostly zonal): shown as `tripResolved: false`,
  OTP logs them as `TRIP_NOT_FOUND`. Route-level info is still correct.
- **CORS `null` on the upstream feeds**: browsers cannot read them; this API is the intermediary.
- OTP graph build on this feed: ≈ 90 s, 178 MB graph, 8,180 stops, 1,508 patterns; serving needs ~4–6 GB heap.
