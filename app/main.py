import asyncio
import datetime as dt
import contextlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from . import __version__
from .admin_auth import PgAdminUserStore, bootstrap_owner
from .admin_config import PgConfigStore, load_overrides
from .analytics import Hasher, PgAnalyticsStore, RateLimiter
from .assistant.budget import BudgetStore, SessionLimiter
from .cities import load_registry
from .config import settings
from .db import close_pool, init_pool
from .errors import install_error_handlers
from .forecast import ForecastCache
from .gbfs import GbfsNetwork
from .gtfs_static import ingest, load_route_index, load_service_index
from .logging_setup import setup_logging
from .normalize import set_feed_flags
from .oidc import OidcService, PgOidcStateStore, configured_providers
from .openmobility import PgOpenMobilityStore, refresh_from_pim, refresh_from_url
from . import geocode as geocode_mod
from .places import PgGeocodeCache, PgPlaceAreaStore, refresh_place_areas
from .push import ApnsClient, PgPushDeviceStore, push_alerts, push_wakes
from .otp import OtpClient
from .routers import (
    admin,
    alerts,
    analytics,
    board,
    chat,
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
    push,
    share,
    stops,
    vehicles,
    watch,
)
from .routers.watch import WatchCache
from .rt import RTCache, poller_loop
from .runtime import CityRuntime
from .share import PgShareStore

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
            rt.gbfs[nid] = GbfsNetwork(rt.city.id, cfg_net, lang=rt.city.locale)


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
            dropped_shares = await app.state.share_store.drop_expired()
            if dropped_shares:
                log.info("share links: dropped %d expired", dropped_shares)
            dropped_sessions = await app.state.admin_users.drop_expired_sessions()
            if dropped_sessions:
                log.info("admin sessions: dropped %d expired", dropped_sessions)
            # Half-finished sign-ins: a person who closed the tab at the provider leaves a row behind.
            dropped_states = await app.state.oidc_states.drop_expired_states()
            if dropped_states:
                log.info("provider sign-ins: dropped %d abandoned", dropped_states)
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
    if om.cds.enabled and om.cds.curbs.source == "pim" and om.cds.curbs.url and om.cds.curbs.provider_id:
        out.append(("pim", om.cds.curbs.url, om.cds.curbs.refresh_minutes))
    if om.mds.enabled and om.mds.authority_url:
        out.append(("mds", om.mds.authority_url, om.mds.refresh_minutes))
    return out


def _utc_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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
                status = app.state.openmobility_sources.setdefault(rt.city.id, {})
                try:
                    if kind == "pim":
                        etags = app.state.openmobility_etags.setdefault(rt.city.id, {})
                        result = await refresh_from_pim(store, rt.city, rt.city.open_mobility.cds.curbs,
                                                        etags=etags)
                    else:
                        result = await refresh_from_url(store, rt.city, url, kind=kind)
                    last[key] = now
                    status[kind] = {"ok": True, "at": _utc_iso(), "error": None, **result}
                    log.info("[%s] %s refreshed from %s: %s", rt.city.id, kind.upper(), url, result)
                except Exception as e:  # noqa: BLE001
                    last[key] = now
                    # the message, never the URL's credentials or a token: a 401 says enough
                    status[kind] = {**status.get(kind, {}), "ok": False, "at": _utc_iso(),
                                    "error": f"{type(e).__name__}: {e}"[:200]}
                    log.exception("[%s] could not refresh %s from %s", rt.city.id, kind.upper(), url)
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=300)


def _apns_for(rt) -> ApnsClient | None:
    cfg = rt.city.config.push
    if not cfg.reminders_active:
        return None
    key = cfg.apns.private_key()
    if not key:
        return None
    return ApnsClient(key_id=cfg.apns.key_id, team_id=cfg.apns.team_id, private_key=key, bundle_id=cfg.apns.bundle_id)


async def _push_loop(app: FastAPI, stop: asyncio.Event) -> None:
    """Every minute: the silent wake-ups that are due, and alert pushes for newly active alerts on the
    routes devices follow. A failing APNs never takes the API down."""
    store = app.state.push_devices
    clients: dict[str, ApnsClient] = {}
    seen: dict[str, set[str]] = {}
    counts: dict[str, dict[str, int]] = {}
    day = ""
    while not stop.is_set():
        today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
        if today != day:
            counts.clear()
            day = today
        for rt in app.state.cities.values():
            cid = rt.city.id
            client = clients.get(cid) or _apns_for(rt)
            if client is None:
                continue
            clients[cid] = client
            status = app.state.push_status.setdefault(cid, {})
            try:
                now = dt.datetime.now(dt.UTC)
                woke = await push_wakes(store, client, cid, now)
                names = {rid: (r.get("short_name") or r.get("shortName") or rid) for rid, r in rt.rt.route_index.items()}
                alerts = rt.rt.active_alerts()
                pushed = await push_alerts(store, client, cid, alerts, names, seen.setdefault(cid, set()),
                                           counts.setdefault(cid, {}))
                status.update({"ok": True, "at": _utc_iso(), "error": None, "sent": client.sent, "failed": client.failed,
                               "lastError": client.last_error})
                if woke or pushed:
                    log.info("[%s] push: %d wake-up(s), %d alert(s)", cid, woke, pushed)
            except Exception as e:  # noqa: BLE001
                status.update({"ok": False, "at": _utc_iso(), "error": f"{type(e).__name__}: {e}"[:200]})
                log.exception("[%s] push pass failed", cid)
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=60)
    for c in clients.values():
        await c.aclose()


async def _place_areas_loop(app: FastAPI, stop: asyncio.Event) -> None:
    """Mirror each city's named areas (barrios, localidades) from its open ArcGIS layers: at start when
    the mirror is missing or older than `refresh_days`, then daily checks. Never takes the API down."""
    store = geocode_mod.areas
    while not stop.is_set():
        for rt in app.state.cities.values():
            cfg = rt.city.geocoder.areas
            if not cfg.active:
                continue
            status = app.state.place_areas_status.setdefault(rt.city.id, {})
            try:
                st = await store.stats(rt.city.id)
                at = dt.datetime.fromisoformat(st["updatedAt"]) if st.get("updatedAt") else None
                fresh = at is not None and (dt.datetime.now(dt.UTC) - at) < dt.timedelta(days=cfg.refresh_days)
                if fresh and st.get("barrios"):
                    status.update({"ok": True, **st})
                    continue
                result = await refresh_place_areas(store, rt.city, cfg)
                status.update({"ok": True, "at": _utc_iso(), "error": None, **result})
                log.info("[%s] place areas refreshed: %s", rt.city.id, result)
            except Exception as e:  # noqa: BLE001
                status.update({"ok": False, "at": _utc_iso(), "error": f"{type(e).__name__}: {e}"[:200]})
                log.exception("[%s] could not refresh place areas", rt.city.id)
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=24 * 3600)


async def _bootstrap_admin(store: PgAdminUserStore, cfg) -> None:
    """First owner on a fresh deployment. Refuses once any account exists, so the variables are safe
    to leave set; the password is read once here and never logged."""
    if not (cfg.ADMIN_BOOTSTRAP_EMAIL and cfg.ADMIN_BOOTSTRAP_PASSWORD):
        return
    try:
        created = await bootstrap_owner(store, cfg.ADMIN_BOOTSTRAP_EMAIL, cfg.ADMIN_BOOTSTRAP_PASSWORD,
                                        cfg.ADMIN_BOOTSTRAP_NAME)
        if created is None:
            log.info("ADMIN_BOOTSTRAP_* ignored: an admin account already exists")
    except Exception:  # noqa: BLE001
        log.exception("could not create the bootstrap owner (start the API and use scripts/admin_user.py)")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = settings()
    setup_logging(cfg.LOG_LEVEL, cfg.LOG_JSON)
    await init_pool()
    app.state.analytics_store = PgAnalyticsStore()
    app.state.analytics_hasher = Hasher(app.state.analytics_store.salt_for)
    app.state.analytics_limiter = RateLimiter(60, 60)
    app.state.share_store = PgShareStore()
    app.state.share_limiter = RateLimiter(30, 60)      # creating shares is rarer than sending events
    app.state.assistant_budget = BudgetStore()
    app.state.assistant_limiter = SessionLimiter(60)
    app.state.forecast_cache = ForecastCache()
    app.state.watch_cache = WatchCache()
    try:
        await app.state.analytics_store.ensure_partitions()
    except Exception:  # noqa: BLE001
        log.exception("could not prepare analytics partitions (ingestion will fail until fixed)")
    registry = load_registry(cfg.CITIES_DIR)
    app.state.cities = {cid: CityRuntime(city=c, rt=RTCache(c), otp=OtpClient(c)) for cid, c in registry.items()}
    app.state.config_store = PgConfigStore()
    app.state.admin_users = PgAdminUserStore()
    await _bootstrap_admin(app.state.admin_users, cfg)
    # Sign in with Google / Microsoft. Both stay off unless their credentials are set: `configured_providers`
    # returns only what this deployment can actually use, and the login screen shows exactly that.
    app.state.oidc_states = PgOidcStateStore()
    app.state.oidc = OidcService(configured_providers(cfg))
    if app.state.oidc.providers:
        log.info("provider sign-in enabled: %s", ", ".join(sorted(app.state.oidc.providers)))
    app.state.openmobility_store = PgOpenMobilityStore()
    app.state.openmobility_sources = {}     # city id -> {kind -> last refresh status}, for /health
    app.state.openmobility_etags = {}       # city id -> {layer -> ETag}: PIM answers 304 when unchanged
    geocode_mod.use_stores(geocode_cache=PgGeocodeCache(), area_store=PgPlaceAreaStore())
    app.state.push_devices = PgPushDeviceStore()
    app.state.push_status = {}              # city id -> last push pass, for /health
    app.state.place_areas_status = {}       # city id -> last mirror status, for /health
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
    if cfg.ENABLE_STATIC_INGEST and any(rt.city.geocoder.areas.active for rt in app.state.cities.values()):
        tasks.append(asyncio.create_task(_place_areas_loop(app, stop), name="places"))
    if cfg.ENABLE_RT_POLLERS and any(rt.city.config.push.reminders_active for rt in app.state.cities.values()):
        tasks.append(asyncio.create_task(_push_loop(app, stop), name="push"))
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
                      {"name": "analytics"}, {"name": "openmobility"}, {"name": "assistant"},
                      {"name": "admin"}],
    )
    cfg = settings()
    origins = [o.strip() for o in cfg.CORS_ORIGINS.split(",") if o.strip()]
    rx = (cfg.CORS_ORIGIN_REGEX or "").strip() or None
    app.add_middleware(CORSMiddleware, allow_origins=[] if rx and not origins else (origins or ["*"]),
                       allow_origin_regex=rx, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    install_error_handlers(app)
    for r in (platform, plan, geocode, stops, board, routes, vehicles, alerts, health, pois, rental, ondemand,
              landing, analytics, openmobility, share, push, watch, chat, admin):
        app.include_router(r.router)

    @app.get("/", include_in_schema=False)
    async def root():
        return {"service": "opentransit-api", "version": __version__, "docs": "/docs", "cities": "/v1/cities"}

    return app


app = create_app()
