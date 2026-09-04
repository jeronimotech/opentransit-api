CITY ?= bogota
PY   ?= .venv/bin/python
PORT ?= 8001

.PHONY: help venv dev up down graph otp otp-stop test lint fmt ingest

help:            ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

venv:            ## create .venv with dev deps (python >= 3.12)
	python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q -r requirements-dev.txt

up:              ## start Postgres/PostGIS (docker compose)
	docker compose up -d postgres

down:            ## stop everything started by compose
	docker compose down

graph:           ## download GTFS + OSM, clip, build the OTP graph for CITY (native on macOS, Docker elsewhere)
	scripts/build-graph.sh $(CITY)

otp:             ## serve the OTP graph for CITY natively on :8080 (or `docker compose up -d otp-$(CITY)`)
	scripts/otp-native.sh serve $(CITY) 8080

otp-stop:        ## stop the native OTP for CITY
	scripts/otp-native.sh stop $(CITY)

dev:             ## run the API with reload on :$(PORT)
	.venv/bin/uvicorn app.main:app --reload --port $(PORT)

ingest:          ## force a static GTFS re-ingest for CITY (needs ADMIN_TOKEN from .env)
	curl -s -X POST -H "X-Admin-Token: $$(grep ADMIN_TOKEN .env | cut -d= -f2)" "localhost:$(PORT)/v1/admin/cities/$(CITY)/ingest-static?force=true"

test:            ## unit tests (no network, no database)
	$(PY) -m pytest -q

lint:            ## ruff
	.venv/bin/ruff check app tests

fmt:             ## ruff --fix + import sorting
	.venv/bin/ruff check --fix app tests
