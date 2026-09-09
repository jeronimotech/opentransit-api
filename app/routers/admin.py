import datetime as dt
import hmac
import logging
import time

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from ..admin_auth import (
    LoginThrottle,
    Principal,
    authenticate,
    can_manage_users,
    hash_password,
    hash_token,
    machine_principal,
    open_session,
    principal_from_user,
    public_user,
    sign_in_with_identity,
    validate_cities,
    validate_email,
    validate_password,
    validate_role,
    visible_cities,
)
from ..admin_config import (
    ConfigPatch,
    apply_to_runtime,
    deep_merge,
    describe,
    effective_city,
    mask_secrets,
    unmask_assistant_patch,
)
from ..config import settings
from ..errors import ApiError, Forbidden, Unauthorized
from ..gtfs_static import ingest, load_route_index, load_service_index
from ..normalize import set_feed_flags
from ..oidc import redirect_uri_for
from ..ondemand import unmask_open_mobility_patch, unmask_patch
from ..runtime import CityRuntime, city_runtime

log = logging.getLogger("ot.admin")
router = APIRouter(tags=["admin"])

SESSION_COOKIE = "ot_admin_session"
_throttle = LoginThrottle()
# Starting a provider sign-in is cheap for us and costs an attacker nothing either, but it does write a
# row; a looser limit than the password throttle keeps a shared office address from locking itself out.
_oidc_throttle = LoginThrottle(limit=30, window_s=300)


# ------------------------------------------------------------------ authentication
def _bearer(request: Request) -> str | None:
    """The web sends the session as a bearer token; a same-origin deployment may send the cookie."""
    auth = request.headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE) or None


async def _machine(request: Request) -> Principal:
    cfg = settings()
    token = request.headers.get("x-admin-token")
    if not token:
        raise Unauthorized("sign in at /v1/admin/auth/login, or send X-Admin-Token")
    if not cfg.ADMIN_TOKEN_ENABLED:
        raise Unauthorized("the machine credential is disabled on this deployment")
    if not hmac.compare_digest(token, cfg.ADMIN_TOKEN):
        raise Unauthorized("missing or invalid X-Admin-Token")
    # Named accounts are the norm now, so every use of the shared secret is worth an audit line.
    log.info("machine credential used for %s %s", request.method, request.url.path)
    return machine_principal()


async def _authenticate(request: Request) -> Principal:
    store = getattr(request.app.state, "admin_users", None)
    token = _bearer(request)
    if token and store is not None:
        row = await store.session(hash_token(token), dt.datetime.now(dt.UTC))
        if row is None or row["disabled"]:
            raise Unauthorized("session expired or revoked")
        await store.touch_session(row["token_hash"])
        return principal_from_user(row, row["token_hash"])
    return await _machine(request)


def _enforce_city(request: Request, p: Principal) -> None:
    city = request.path_params.get("city")
    if city and not p.may_access(city):
        raise Forbidden(f"your account is not scoped to {city}")


async def require_admin(request: Request) -> Principal:
    """
    The floor for every admin route: authenticated, and in scope for the `{city}` in the path.
    Routes that change something ask for `require_editor` on top of this.
    """
    p = await _authenticate(request)
    _enforce_city(request, p)
    return p


async def require_editor(request: Request) -> Principal:
    p = await require_admin(request)
    if not p.has_role("admin"):
        raise Forbidden("this action needs the admin role")
    return p


async def require_owner(request: Request) -> Principal:
    p = await _authenticate(request)
    if not can_manage_users(p):
        raise Forbidden("only a signed-in owner may manage accounts")
    return p


def _store(request: Request):
    store = getattr(request.app.state, "admin_users", None)
    if store is None:
        raise ApiError("accounts are not available on this deployment", status=503, code="UNAVAILABLE")
    return store


# ------------------------------------------------------------------ sign in / out
class LoginBody(BaseModel):
    email: str
    password: str


@router.post("/v1/admin/auth/login")
async def login(body: LoginBody, request: Request):
    """
    Returns the session token exactly once. The web keeps it in an httpOnly cookie on its own origin;
    a script may keep it in memory and send `Authorization: Bearer`.
    """
    store = _store(request)
    now = time.time()
    email = (body.email or "").strip().lower()
    keys = [f"e:{email}", f"a:{request.client.host if request.client else '?'}"]
    if any(_throttle.blocked(k, now) for k in keys):
        raise ApiError("too many failed sign-ins; wait a few minutes", status=429, code="RATE_LIMITED")
    user = await authenticate(store, email, body.password or "")
    if user is None:
        for k in keys:
            _throttle.fail(k, now)
        log.info("failed admin sign-in for %s", email or "(no email)")
        raise Unauthorized("wrong email or password")
    for k in keys:
        _throttle.clear(k)
    log.info("admin sign-in: %s (%s)", user["email"], user["role"])
    return await _issue_session(request, store, user)


async def _issue_session(request: Request, store, user: dict) -> dict:
    """
    The one place a session is minted. Password sign-in and provider sign-in both end here, so a
    session issued by Google is the same `admin_session` row as one issued by a password: same roles,
    same city scope, same revocation, same cookie proxy. The token is returned exactly once.
    """
    token, expires_at = await open_session(store, user, hours=settings().ADMIN_SESSION_HOURS,
                                           user_agent=request.headers.get("user-agent"))
    return {"token": token, "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
            "user": public_user(user), "cities": visible_cities(principal_from_user(user),
                                                                sorted(request.app.state.cities))}


@router.post("/v1/admin/auth/logout")
async def logout(request: Request, me: Principal = Depends(_authenticate)):
    """Revokes this session only. A machine credential has no session to revoke, so it is a no-op."""
    if me.session_hash:
        await _store(request).delete_session(me.session_hash)
    return {"ok": True}


@router.get("/v1/admin/auth/me")
async def auth_me(request: Request, me: Principal = Depends(_authenticate)):
    return {"ok": True, "user": me.public(), "cities": visible_cities(me, sorted(request.app.state.cities)),
            "canManageUsers": can_manage_users(me)}


@router.get("/v1/admin/me")
async def admin_me(request: Request, me: Principal = Depends(_authenticate)):
    """Kept for scripts and older clients; `/v1/admin/auth/me` is the same answer."""
    return await auth_me(request, me)


# ------------------------------------------------------------------ sign in with Google / Microsoft
# Three unauthenticated endpoints, because signing in is by definition what you do before you have a
# session. The browser never talks to them directly: the web's own server-side route handlers do, so
# the browser token that binds the flow stays in an httpOnly cookie. See app/oidc.py for the rules.
def _oidc(request: Request):
    svc = getattr(request.app.state, "oidc", None)
    if svc is None or not svc.providers:
        raise ApiError("provider sign-in is not configured on this deployment", status=404,
                       code="NOT_FOUND")
    return svc


def _states(request: Request):
    states = getattr(request.app.state, "oidc_states", None)
    if states is None:
        raise ApiError("provider sign-in is not available on this deployment", status=503,
                       code="UNAVAILABLE")
    return states


def _provisioner(request: Request):
    """None unless this deployment explicitly opted into domain provisioning — which it should not."""
    from ..admin_auth import domain_provisioner
    cfg = settings()
    return domain_provisioner(
        _store(request),
        domains=[d for d in (cfg.OIDC_AUTO_PROVISION_DOMAINS or "").split(",") if d.strip()],
        role=validate_role((cfg.OIDC_AUTO_PROVISION_ROLE or "viewer").strip()),
        cities=validate_cities([c for c in (cfg.OIDC_AUTO_PROVISION_CITIES or "").split(",") if c.strip()],
                               set(request.app.state.cities)))


@router.get("/v1/admin/auth/providers")
async def auth_providers(request: Request):
    """What the login screen may offer. A provider that is not configured is simply not in this list."""
    svc = getattr(request.app.state, "oidc", None)
    return {"password": True, "providers": svc.public() if svc is not None else []}


@router.post("/v1/admin/auth/oidc/{provider}/start")
async def oidc_start(provider: str, request: Request):
    """
    Begins the flow: mints `state`, a PKCE verifier and a nonce, stores them for a few minutes, and
    returns the provider's authorization URL plus the browser token the caller must keep. The
    `redirect_uri` is computed from this deployment's configuration and never accepted from the
    caller — one that a caller can choose is an open redirect with an authorization code attached.
    """
    svc, cfg = _oidc(request), settings()
    svc.provider(provider)          # 404 for a provider this deployment has not configured
    key = f"o:{request.client.host if request.client else '?'}"
    now = time.time()
    if _oidc_throttle.blocked(key, now):
        raise ApiError("too many sign-in attempts; wait a few minutes", status=429, code="RATE_LIMITED")
    _oidc_throttle.fail(key, now)
    return await svc.start(_states(request), provider, redirect_uri=redirect_uri_for(cfg, provider),
                           ttl_s=cfg.OIDC_STATE_TTL_SECONDS)


class OidcCallbackBody(BaseModel):
    state: str
    code: str
    browserToken: str      # noqa: N815 — the wire shape is camelCase like every other admin response


@router.post("/v1/admin/auth/oidc/{provider}/callback")
async def oidc_callback(provider: str, body: OidcCallbackBody, request: Request):
    """
    Finishes the flow. Everything that could go wrong before this line — a replayed `state`, a token
    from another browser, a forged id_token — has already been refused in `OidcService.complete`; what
    is left is the question this endpoint exists to answer: is there an account for this person?
    """
    svc, store, cfg = _oidc(request), _store(request), settings()
    ident = await svc.complete(_states(request), provider, state=body.state, code=body.code,
                               browser_token=body.browserToken,
                               redirect_uri=redirect_uri_for(cfg, provider))
    user = await sign_in_with_identity(store, ident, provision=_provisioner(request))
    log.info("admin sign-in via %s: %s (%s)", provider, user["email"], user["role"])
    return await _issue_session(request, store, user)


# ------------------------------------------------------------------ accounts (owner only)
class UserCreate(BaseModel):
    email: str
    password: str
    name: str = ""
    role: str = "viewer"
    cities: list[str] | None = None


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    cities: list[str] | None = None
    disabled: bool | None = None
    password: str | None = None


async def _last_owner(store, user_id: int) -> bool:
    """A deployment that loses its last enabled owner can only be rescued from a shell."""
    owners = [u for u in await store.list_users() if u["role"] == "owner" and not u["disabled"]]
    return [u["id"] for u in owners] == [user_id]


@router.get("/v1/admin/users")
async def list_users(request: Request, _: Principal = Depends(require_owner)):
    return {"users": [public_user(u) for u in await _store(request).list_users()]}


@router.post("/v1/admin/users", status_code=201)
async def create_user(body: UserCreate, request: Request, _: Principal = Depends(require_owner)):
    store = _store(request)
    validate_password(body.password)
    validate_email(body.email)
    # The address is stored as typed (the database lower-cases a copy for the unique index), so a person
    # sees their own capitalisation back while sign-in stays case-insensitive.
    row = await store.create_user(email=body.email.strip(), password_hash=hash_password(body.password),
                                  name=(body.name or "").strip()[:120], role=validate_role(body.role),
                                  cities=validate_cities(body.cities, set(request.app.state.cities)))
    log.info("admin account created: %s (%s)", row["email"], row["role"])
    return public_user(row)


@router.patch("/v1/admin/users/{user_id}")
async def update_user(user_id: int, body: UserPatch, request: Request,
                      me: Principal = Depends(require_owner)):
    store = _store(request)
    if await store.by_id(user_id) is None:
        raise ApiError(f"no account with id {user_id}", status=404, code="NOT_FOUND")
    fields: dict = {}
    if "name" in body.model_fields_set:
        fields["name"] = (body.name or "").strip()[:120]
    if "role" in body.model_fields_set and body.role is not None:
        fields["role"] = validate_role(body.role)
    if "cities" in body.model_fields_set:
        fields["cities"] = validate_cities(body.cities, set(request.app.state.cities))
    if "disabled" in body.model_fields_set and body.disabled is not None:
        fields["disabled"] = bool(body.disabled)
    if fields.get("disabled") or (fields.get("role") and fields["role"] != "owner"):
        if await _last_owner(store, user_id):
            raise ApiError("this is the last enabled owner", status=409, code="CONFLICT")
    if "password" in body.model_fields_set and body.password is not None:
        validate_password(body.password)
        fields["password_hash"] = hash_password(body.password)
    row = await store.update_user(user_id, **fields)
    # A new password, a smaller role or a smaller city scope must not wait for the old session to lapse.
    # That includes the caller's own session: changing your own password signs you out, as it should.
    if {"password_hash", "role", "cities", "disabled"} & set(fields):
        await store.delete_sessions_of(user_id)
    log.info("admin account updated: %s by %s (%s)", row["email"], me.label,
             ", ".join(sorted(fields)) or "no change")
    return public_user(row)


@router.post("/v1/admin/users/{user_id}/disable")
async def disable_user(user_id: int, request: Request, me: Principal = Depends(require_owner)):
    """Accounts are disabled, never deleted: the history rows that name them stay readable."""
    store = _store(request)
    if await store.by_id(user_id) is None:
        raise ApiError(f"no account with id {user_id}", status=404, code="NOT_FOUND")
    if await _last_owner(store, user_id):
        raise ApiError("this is the last enabled owner", status=409, code="CONFLICT")
    row = await store.update_user(user_id, disabled=True)
    await store.delete_sessions_of(user_id)
    log.info("admin account disabled: %s (by %s)", row["email"], me.label)
    return public_user(row)


# ------------------------------------------------------------------ city operations
@router.post("/v1/admin/cities/{city}/ingest-static", dependencies=[Depends(require_editor)])
async def ingest_static(rt: CityRuntime = Depends(city_runtime), force: bool = False):
    result = await ingest(rt.city, force=force)
    rt.rt.set_static(*await load_route_index(rt.city))
    rt.services = await load_service_index(rt.city)
    set_feed_flags(rt.city.id, rt.services.flags)
    rt.static_ready = True
    return result


@router.post("/v1/admin/cities/{city}/purge", dependencies=[Depends(require_editor)])
async def purge(rt: CityRuntime = Depends(city_runtime)):
    """Drop in-memory vehicle history for this city (the only per-city state that grows)."""
    n = len(rt.rt.history)
    rt.rt.history.clear()
    return {"purgedVehicles": n}


# ------------------------------------------------------------------ editable city configuration
@router.get("/v1/admin/cities/{city}/config", dependencies=[Depends(require_admin)])
async def get_config(rt: CityRuntime = Depends(city_runtime)):
    return describe(rt)


@router.put("/v1/admin/cities/{city}/config")
async def put_config(patch: ConfigPatch, request: Request, rt: CityRuntime = Depends(city_runtime),
                     me: Principal = Depends(require_editor)):
    """Partial deep-merge into the stored override. A JSON null for a section (or a key) removes that override
    so the YAML value applies again. The effective result is validated strictly before anything is saved."""
    sections = {k: v for k, v in patch.model_dump(exclude={"note", "updatedBy"}).items()
                if k in patch.model_fields_set}
    if sections.get("mobility"):
        # masked credentials echoed back by the UI keep their stored value; new values are stored as sent
        sections["mobility"] = unmask_patch(sections["mobility"], rt.city)
    if sections.get("openMobility"):
        sections["openMobility"] = unmask_open_mobility_patch(sections["openMobility"], rt.city)
    if sections.get("config"):
        sections["config"] = unmask_assistant_patch(sections["config"], rt.city, rt.base_city or rt.city)
    new_override = deep_merge(rt.override or {}, sections)
    effective_city(rt.base_city or rt.city, new_override)          # raises 422 with the field path
    # Who signed in beats whatever the client typed: the audit trail is only worth reading if it is true.
    row = await request.app.state.config_store.save(rt.city.id, new_override, _author(me, patch.updatedBy),
                                                    patch.note)
    apply_to_runtime(rt, row)
    _sync_gbfs(rt)
    return describe(rt)


@router.delete("/v1/admin/cities/{city}/config")
async def delete_config(request: Request, rt: CityRuntime = Depends(city_runtime),
                        me: Principal = Depends(require_editor),
                        updatedBy: str | None = Query(None, max_length=120)):
    row = await request.app.state.config_store.clear(rt.city.id, _author(me, updatedBy))
    apply_to_runtime(rt, row)
    _sync_gbfs(rt)
    return describe(rt)


@router.get("/v1/admin/cities/{city}/config/history", dependencies=[Depends(require_admin)])
async def config_history(request: Request, rt: CityRuntime = Depends(city_runtime),
                         limit: int = Query(20, ge=1, le=200)):
    items = await request.app.state.config_store.history(rt.city.id, limit)
    return {"items": [{**i, "data": mask_secrets(i.get("data"))} for i in items]}


def _author(me: Principal, claimed: str | None) -> str | None:
    """A person is named by their account; only the shared machine credential may claim a name."""
    return claimed if me.kind == "machine" else me.label


def _sync_gbfs(rt: CityRuntime) -> None:
    from ..main import sync_gbfs  # local import: main imports this router
    sync_gbfs(rt)
