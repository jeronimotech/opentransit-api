import asyncio
import contextlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from . import __version__
from .admin_config import PgConfigStore, load_overrides
from .analytics import Hasher, PgAnalyticsStore, RateLimiter
from .cities import load_registry
from .config import settings
from .db import close_pool, init_pool
from .errors import install_error_handlers
from .gbfs import GbfsNetwork
from .gtfs_static import ingest, load_route_index, load_service_index
from .logging_setup import setup_logging
from .normalize import set_feed_flags
from .openmobility import PgOpenMobilityStore, refresh_from_url
from .otp import OtpClient
from .routers import (
    admin,
    alerts,
    analytics,
    board,
    geocode,
    health,
    landing,
    ondemand,
    openmobility,
    plan,
    platform,
    pois,
    rental,
    routes,
    stops,
    vehicles,
)
from .rt import RTCache, poller_loop
from .runtime import CityRuntime

log = logging.getLogger("ot.main")


def sync_gbfs(rt: CityRuntime) -> None:
    """Make rt.gbfs match the (effective) city config: add new networks, drop removed ones, keep the rest warm."""
    wanted = {n.id: n for n in rt.city.mobility.bike_share}
    for nid in list(rt.gbfs):
        cur = rt.gbfs[nid]
        if nid not in wanted or wanted[nid].gbfs_url != cur.cfg.gbfs_url:
            asyncio.create_task(cur.close())
            del rt.gbfs[nid]
    for nid, cfg_net in wanted.items():
        if nid in rt.gbfs:
            rt.gbfs[nid].cfg = cfg_net
        else:
            rt.gbfs[nid] = GbfsNetwork(rt.city.id, cfg_net)


async def _bootstrap_static(rt: CityRuntime, do_ingest: bool) -> None:
    try:
        if do_ingest:
            await ingest(rt.city)
        rt.rt.set_static(*await load_route_index(rt.city))
        rt.services = await load_service_index(rt.city)
        set_feed_flags(rt.city.id, rt.services.flags)
        rt.static_ready = bool(rt.rt.route_index)
        rt.ingest_error = None
    except Exception as e:  # noqa: BLE001
        rt.ingest_error = str(e)
        log.exception("[%s] static bootstrap failed (continuing without it)", rt.city.id)


async def _static_loop(rt: CityRuntime, stop: asyncio.Event) -> None:
    hours = rt.city.feeds.static_refresh_hours
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=hours * 3600)
            return
        except TimeoutError:
            pass
        await _bootstrap_static(rt, True)


async def _analytics_loop(app: FastAPI, stop: asyncio.Event) -> None:
    """Every ANALYTICS_ROLLUP_SECONDS: rollup each city; once a day: partitions ahead + retention drop."""
    cfg = settings()
    store: PgAnalyticsStore = app.state.analytics_store
    last_maint = 0.0
    while not stop.is_set():
        try:
            import time
            if time.time() - last_maint > 6 * 3600:
                await store.ensure_partitions()
                for rt in app.state.cities.values():
                    dropped = await store.drop_expired(rt.city.config.analytics.retention_days)
                    if dropped:
                        log.info("[%s] analytics retention: dropped %s", rt.city.id, dropped)
                last_maint = time.time()
            for rt in app.state.cities.values():
                if rt.city.config.analytics.enabled:
                    r = await store.rollup(rt.city)
                    if r["events"]:
                        log.info("[%s] analytics rollup: %d events, %d days", rt.city.id, r["events"], r["days"])
        except Exception:  # noqa: BLE001
            log.exception("analytics job failed (will retry)")
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.ANALYTICS_ROLLUP_SECONDS)
        except TimeoutError:
            pass


def _om_sources(rt) -> list[tuple[str, str, int]]:
    """(kind, url, refresh_minutes) for each third-party document this city mirrors."""
    om = rt.city.open_mobility
    out = []
    if om.cds.enabled and om.cds.curbs.source == "url" and om.cds.curbs.url:
        out.append(("cds", om.cds.curbs.url, om.cds.curbs.refresh_minutes))
    if om.mds.enabled and om.mds.authority_url:
        out.append(("mds", om.mds.authority_url, om.mds.refresh_minutes))
    return out


async def _open_mobility_loop(app: FastAPI, stop: asyncio.Event) -> None:
    """Mirror the configured CDS / MDS documents. A failing upstream never takes the API down."""
    store = app.state.openmobility_store
    last: dict[tuple[str, str], float] = {}
    while not stop.is_set():
        now = asyncio.get_running_loop().time()
        for rt in app.state.cities.values():
            for kind, url, minutes in _om_sources(rt):
                key = (rt.city.id, kind)
                if now - last.get(key, -1e9) < minutes * 60:
                    continue
                try:
                    result = await refresh_from_url(store, rt.city, url, kind=kind)
                    last[key] = now
                    log.info("[%s] %s refreshed from %s: %s", rt.city.id, kind.upper(), url, result)
                except Exception:  # noqa: BLE001
                    last[key] = now
                    log.exception("[%s] could not refresh %s from %s", rt.city.id, kind.upper(), url)
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=300)

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = settings()
    setup_logging(cfg.LOG_LEVEL, cfg.LOG_JSON)
    await init_pool()
    app.state.analytics_store = PgAnalyticsStore()
    app.state.analytics_hasher = Hasher(app.state.analytics_store.salt_for)
    app.state.analytics_limiter = RateLimiter(60, 60)
    try:
        await app.state.analytics_store.ensure_partitions()
    except Exception:  # noqa: BLE001
        log.exception("could not prepare analytics partitions (ingestion will fail until fixed)")
    registry = load_registry(cfg.CITIES_DIR)
    app.state.cities = {cid: CityRuntime(city=c, rt=RTCache(c), otp=OtpClient(c)) for cid, c in registry.items()}
    app.state.config_store = PgConfigStore()
    app.state.openmobility_store = PgOpenMobilityStore()
    await load_overrides(app.state.config_store, app.state.cities)
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    for rt in app.state.cities.values():
        sync_gbfs(rt)
        if cfg.ENABLE_RT_POLLERS:
            for g in rt.gbfs.values():
                tasks.append(asyncio.create_task(g.poll_loop(stop), name=f"gbfs:{rt.city.id}:{g.cfg.id}"))
        # Static ingest downloads ~100 MB from a third party; it must never block start-up.
        do_ingest = cfg.ENABLE_STATIC_INGEST and cfg.STATIC_INGEST_ON_START
        tasks.append(asyncio.create_task(_bootstrap_static(rt, do_ingest), name=f"bootstrap:{rt.city.id}"))
        if cfg.ENABLE_STATIC_INGEST:
            tasks.append(asyncio.create_task(_static_loop(rt, stop), name=f"static:{rt.city.id}"))
        f = rt.city.feeds
        if cfg.ENABLE_RT_POLLERS and (f.rt_positions_url or f.rt_tripupdates_url or f.rt_alerts_url):
            tasks.append(asyncio.create_task(poller_loop(rt.rt, stop), name=f"rt:{rt.city.id}"))
        asyncio.create_task(rt.otp.server_info())
    if cfg.ENABLE_ANALYTICS_JOBS:
        tasks.append(asyncio.create_task(_analytics_loop(app, stop), name="analytics"))
    if cfg.ENABLE_RT_POLLERS and any(_om_sources(rt) for rt in app.state.cities.values()):
        tasks.append(asyncio.create_task(_open_mobility_loop(app, stop), name="openmobility"))
    log.info("opentransit-api %s up · %d cities · %d background tasks", __version__, len(registry), len(tasks))
    try:
        yield
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for rt in app.state.cities.values():
            await rt.otp.close()
            for g in rt.gbfs.values():
                await g.close()
        await close_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="opentransit-api",
        description="Open-source, multi-city, multimodal trip-planning API (GTFS + GTFS-RT + OpenTripPlanner).",
        version=__version__, lifespan=lifespan,
        openapi_tags=[{"name": "planning"}, {"name": "search"}, {"name": "stops"}, {"name": "routes"},
                      {"name": "realtime"}, {"name": "rental"}, {"name": "ondemand"}, {"name": "platform"},
                      {"name": "analytics"}, {"name": "openmobility"},
                      {"name": "admin"}],
    )
    origins = [o.strip() for o in settings().CORS_ORIGINS.split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins or ["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    install_error_handlers(app)
    for r in (platform, plan, geocode, stops, board, routes, vehicles, alerts, health, pois, rental, ondemand,
              landing, analytics, openmobility, admin):
        app.include_router(r.router)

    @app.get("/", include_in_schema=False)
    async def root():
        return {"service": "opentransit-api", "version": __version__, "docs": "/docs", "cities": "/v1/cities"}

    return app


app = create_app()
