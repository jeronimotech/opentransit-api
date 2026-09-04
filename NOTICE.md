# Notices

- The GTFS-Realtime poller (`app/rt.py`), the selective static-GTFS ingest (`app/gtfs_static.py`), the
  polyline/Douglas-Peucker helpers (`app/geo.py`) and the gzip-flushed SSE stream were ported and
  generalized from **SIRCI Live** (an internal TransMilenio S.A. GTFS-RT observability prototype, 2026),
  written by the same author and relicensed here under MIT.
- Routing is provided by [OpenTripPlanner](https://www.opentripplanner.org/) (LGPL-3.0), run as a
  separate process; this project does not modify or embed it.
- Bogotá transit data: TRANSMILENIO S.A. (GTFS / GTFS-RT). Street data: © OpenStreetMap contributors (ODbL).
- Geocoding of addresses/POIs: [Photon](https://photon.komoot.io) by komoot, backed by OpenStreetMap.
