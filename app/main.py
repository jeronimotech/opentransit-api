import asyncio
import contextlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from . import __version__
from .admin_config import PgConfigStore, load_overrides
from .cities import load_registry
from .config import settings
from .db import close_pool, init_pool
from .errors import install_error_handlers
from .gtfs_static import ingest, load_route_index, load_service_index
from .logging_setup import setup_logging
from .normalize import set_feed_flags
from .otp import OtpClient
from .routers import admin, alerts, board, geocode, health, plan, platform, pois, routes, stops, vehicles
from .rt import RTCache, poller_loop
from .runtime import CityRuntime

log = logging.getLogger("ot.main")


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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = settings()
    setup_logging(cfg.LOG_LEVEL, cfg.LOG_JSON)
    await init_pool()
    registry = load_registry(cfg.CITIES_DIR)
    app.state.cities = {cid: CityRuntime(city=c, rt=RTCache(c), otp=OtpClient(c)) for cid, c in registry.items()}
    app.state.config_store = PgConfigStore()
    await load_overrides(app.state.config_store, app.state.cities)
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    for rt in app.state.cities.values():
        # Static ingest downloads ~100 MB from a third party; it must never block start-up.
        do_ingest = cfg.ENABLE_STATIC_INGEST and cfg.STATIC_INGEST_ON_START
        tasks.append(asyncio.create_task(_bootstrap_static(rt, do_ingest), name=f"bootstrap:{rt.city.id}"))
        if cfg.ENABLE_STATIC_INGEST:
            tasks.append(asyncio.create_task(_static_loop(rt, stop), name=f"static:{rt.city.id}"))
        f = rt.city.feeds
        if cfg.ENABLE_RT_POLLERS and (f.rt_positions_url or f.rt_tripupdates_url or f.rt_alerts_url):
            tasks.append(asyncio.create_task(poller_loop(rt.rt, stop), name=f"rt:{rt.city.id}"))
        asyncio.create_task(rt.otp.server_info())
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
        await close_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="opentransit-api",
        description="Open-source, multi-city, multimodal trip-planning API (GTFS + GTFS-RT + OpenTripPlanner).",
        version=__version__, lifespan=lifespan,
        openapi_tags=[{"name": "planning"}, {"name": "search"}, {"name": "stops"}, {"name": "routes"},
                      {"name": "realtime"}, {"name": "platform"}, {"name": "admin"}],
    )
    origins = [o.strip() for o in settings().CORS_ORIGINS.split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins or ["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    install_error_handlers(app)
    for r in (platform, plan, geocode, stops, board, routes, vehicles, alerts, health, pois, admin):
        app.include_router(r.router)

    @app.get("/", include_in_schema=False)
    async def root():
        return {"service": "opentransit-api", "version": __version__, "docs": "/docs", "cities": "/v1/cities"}

    return app


app = create_app()
