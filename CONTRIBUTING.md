# Contributing

Thanks for helping build an open trip planner for every city.

## Set up
```bash
make venv && make up            # Python 3.12 venv, Postgres/PostGIS in Docker
make graph CITY=bogota          # ~2 min on a laptop: downloads GTFS + OSM, builds the OTP graph
make otp                        # serve it on :8080
cp .env.example .env && make dev
```

## Rules of the road
- **Nothing city-specific in `app/`.** Anything that differs per city belongs in `cities/<city>.yaml` or
  `otp/<city>/`. If you catch yourself writing `if city.id == "bogota"`, stop and add a config knob.
- **The contract is `docs/API.md`.** Change it in the same PR as the code, and keep the Pydantic models in
  `app/models.py` in sync; `/docs` must stay accurate.
- **Never leak raw OTP.** Everything from OpenTripPlanner goes through `app/normalize.py`.
- Tests must not touch the network or a database (`make test`). Anything that needs live data goes in a
  script under `scripts/`, not in `tests/`.
- Run `make lint` before pushing. CI runs ruff, pytest and a Docker build.

## Adding a city
See the README section "Add a city in five steps". Open a PR with the two config files and a note on the
feed's quirks (`docs/cities/<city>.md`), so app developers know what to expect.

## Reporting data problems
Data bugs (wrong stop names, missing routes) are usually upstream in the agency's GTFS. Please check the
feed first and report to the agency; open an issue here only if our ingest or normalization mangles it.
