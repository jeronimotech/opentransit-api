"""v1.11 named admin accounts: hashing, sessions, roles, city scope, and the machine credential."""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admin_auth import (
    LoginThrottle,
    MemoryAdminUserStore,
    Principal,
    authenticate,
    bootstrap_owner,
    can_manage_users,
    hash_password,
    hash_token,
    machine_principal,
    needs_rehash,
    new_session_token,
    normalize_email,
    open_session,
    principal_from_user,
    public_user,
    token_matches,
    verify_password,
    visible_cities,
)
from app.admin_config import MemoryConfigStore
from app.cities import City
from app.config import settings
from app.errors import ApiError, install_error_handlers
from app.routers import admin
from app.rt import RTCache
from app.runtime import CityRuntime

MACHINE = {"X-Admin-Token": "test-token"}
PW = "correct horse battery"          # >= MIN_PASSWORD_LEN


def _app(bogota: City, cities: tuple[str, ...] = ("bogota",)) -> tuple[FastAPI, MemoryAdminUserStore]:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin.router)
    runtimes = {}
    for cid in cities:
        city = bogota if cid == "bogota" else bogota.model_copy(update={"id": cid})
        runtimes[cid] = CityRuntime(city=city, rt=RTCache(city), otp=None)  # type: ignore[arg-type]
    app.state.cities = runtimes
    app.state.config_store = MemoryConfigStore()
    store = MemoryAdminUserStore()
    app.state.admin_users = store
    return app, store


async def _user(store: MemoryAdminUserStore, email: str, role: str, cities: list[str] | None = None,
                password: str = PW) -> dict:
    return await store.create_user(email=email, password_hash=hash_password(password), name=email.split("@")[0],
                                   role=role, cities=cities or [])


async def _signin(c: AsyncClient, email: str, password: str = PW) -> dict:
    r = await c.post("/v1/admin/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ------------------------------------------------------------------ passwords
def test_password_is_stored_as_an_argon2id_digest_not_the_password():
    h = hash_password(PW)
    assert h.startswith("$argon2id$") and PW not in h
    assert verify_password(PW, h)
    assert not verify_password("wrong", h)
    assert not verify_password("", h)
    assert hash_password(PW) != h            # a fresh salt every time
    assert not needs_rehash(h)
    assert needs_rehash("not-a-hash")


def test_short_passwords_are_refused():
    with pytest.raises(ApiError) as e:
        hash_password("short")
    assert e.value.status == 422 and e.value.message.startswith("password:")


def test_email_identity_is_case_insensitive():
    assert normalize_email("  Luis@Example.COM ") == "luis@example.com"


def test_public_user_never_carries_the_hash():
    row = {"id": 1, "email": "a@b.co", "name": "A", "role": "owner", "cities": ["bogota"], "disabled": False,
           "password_hash": hash_password(PW), "created_at": None, "last_login_at": None}
    assert "password_hash" not in public_user(row) and "passwordHash" not in public_user(row)


# ------------------------------------------------------------------ session tokens
def test_session_token_is_stored_only_as_a_digest():
    token = new_session_token()
    stored = hash_token(token)
    assert token not in stored and len(stored) == 64
    assert token_matches(token, stored)
    assert not token_matches("guess", stored) and not token_matches(None, stored)
    assert len({new_session_token() for _ in range(200)}) == 200


async def test_authenticate_rejects_wrong_password_and_disabled_accounts():
    store = MemoryAdminUserStore()
    user = await _user(store, "luis@example.com", "owner")
    assert await authenticate(store, "LUIS@example.com", PW) is not None
    assert await authenticate(store, "luis@example.com", "nope") is None
    assert await authenticate(store, "nobody@example.com", PW) is None
    await store.update_user(user["id"], disabled=True)
    assert await authenticate(store, "luis@example.com", PW) is None


async def test_session_expiry_and_revocation():
    store = MemoryAdminUserStore()
    user = await _user(store, "luis@example.com", "owner")
    token, expires_at = await open_session(store, user, hours=12, user_agent="pytest")
    now = dt.datetime.now(dt.UTC)
    assert expires_at > now
    assert (await store.session(hash_token(token), now))["email"] == "luis@example.com"
    assert await store.session(hash_token(token), expires_at + dt.timedelta(seconds=1)) is None
    assert await store.delete_session(hash_token(token)) is True
    assert await store.session(hash_token(token), now) is None
    assert (await store.by_id(user["id"]))["last_login_at"] is not None


async def test_disabling_a_user_can_revoke_every_session():
    store = MemoryAdminUserStore()
    user = await _user(store, "luis@example.com", "admin")
    await open_session(store, user, hours=12, user_agent=None)
    await open_session(store, user, hours=12, user_agent=None)
    assert await store.delete_sessions_of(user["id"]) == 2


# ------------------------------------------------------------------ pure authorisation rules
def test_role_ranking_and_city_scope():
    viewer = Principal("user", role="viewer", cities=("bogota",))
    editor = Principal("user", role="admin")
    owner = Principal("user", role="owner")
    assert viewer.has_role("viewer") and not viewer.has_role("admin")
    assert editor.has_role("admin") and not editor.has_role("owner")
    assert owner.has_role("owner")
    assert viewer.may_access("bogota") and not viewer.may_access("medellin")
    assert editor.may_access("medellin")          # empty scope means every city
    assert visible_cities(viewer, ["bogota", "medellin"]) == ["bogota"]


def test_only_a_signed_in_owner_manages_accounts():
    assert can_manage_users(Principal("user", role="owner"))
    assert not can_manage_users(Principal("user", role="admin"))
    assert not can_manage_users(machine_principal())     # a shared secret never creates people
    assert machine_principal().has_role("admin")


def test_login_throttle_forgets_on_success():
    th = LoginThrottle(limit=3, window_s=60)
    for i in range(3):
        assert not th.blocked("k", 100.0 + i)
        th.fail("k", 100.0 + i)
    assert th.blocked("k", 103.0)
    assert not th.blocked("k", 1000.0)               # the window slid past
    th.fail("k", 1000.0)
    th.clear("k")
    assert not th.blocked("k", 1000.0)


# ------------------------------------------------------------------ endpoints
async def test_unauthenticated_requests_are_refused(bogota: City):
    app, _ = _app(bogota)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for method, path in (("get", "/v1/admin/auth/me"), ("get", "/v1/admin/cities/bogota/config"),
                             ("get", "/v1/admin/users"), ("post", "/v1/admin/cities/bogota/purge")):
            r = await getattr(c, method)(path)
            assert r.status_code == 401, (path, r.text)
            assert r.json()["error"]["code"] == "UNAUTHORIZED"
        r = await c.get("/v1/admin/auth/me", headers={"Authorization": "Bearer not-a-session"})
        assert r.status_code == 401


async def test_sign_in_returns_a_session_once_and_never_the_password(bogota: City):
    app, store = _app(bogota)
    await _user(store, "luis@example.com", "owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/admin/auth/login", json={"email": "Luis@Example.com", "password": PW})
        assert r.status_code == 200
        body = r.json()
        assert PW not in r.text and "passwordHash" not in r.text
        assert body["user"]["role"] == "owner" and body["cities"] == ["bogota"]
        assert hash_token(body["token"]) in store.sessions

        h = {"Authorization": f"Bearer {body['token']}"}
        me = (await c.get("/v1/admin/auth/me", headers=h)).json()
        assert me["user"]["email"] == "luis@example.com" and me["canManageUsers"] is True
        assert (await c.get("/v1/admin/me", headers=h)).json() == me

        assert (await c.post("/v1/admin/auth/logout", headers=h)).json() == {"ok": True}
        assert (await c.get("/v1/admin/auth/me", headers=h)).status_code == 401

        r = await c.post("/v1/admin/auth/login", json={"email": "luis@example.com", "password": "wrong"})
        assert r.status_code == 401 and r.json()["error"]["message"] == "wrong email or password"


async def test_an_expired_session_is_refused(bogota: City):
    app, store = _app(bogota)
    user = await _user(store, "luis@example.com", "admin")
    token = new_session_token()
    await store.create_session(hash_token(token), user["id"],
                               dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1), None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/admin/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401 and "expired" in r.json()["error"]["message"]


async def test_roles_are_enforced_per_route(bogota: City):
    app, store = _app(bogota)
    await _user(store, "owner@example.com", "owner")
    await _user(store, "editor@example.com", "admin")
    await _user(store, "viewer@example.com", "viewer")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        viewer = await _signin(c, "viewer@example.com")
        editor = await _signin(c, "editor@example.com")
        owner = await _signin(c, "owner@example.com")

        # viewer reads, but never writes
        assert (await c.get("/v1/admin/cities/bogota/config", headers=viewer)).status_code == 200
        r = await c.put("/v1/admin/cities/bogota/config", headers=viewer, json={"fares": {"base": 3400}})
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
        assert (await c.post("/v1/admin/cities/bogota/purge", headers=viewer)).status_code == 403
        assert (await c.get("/v1/admin/users", headers=viewer)).status_code == 403

        # admin writes config, but never accounts
        r = await c.put("/v1/admin/cities/bogota/config", headers=editor, json={"fares": {"base": 3400}})
        assert r.status_code == 200 and r.json()["effective"]["fares"]["base"] == 3400
        assert (await c.post("/v1/admin/cities/bogota/purge", headers=editor)).status_code == 200
        assert (await c.get("/v1/admin/users", headers=editor)).status_code == 403

        # owner does everything
        assert (await c.get("/v1/admin/users", headers=owner)).status_code == 200


async def test_the_signed_in_account_names_the_change(bogota: City):
    app, store = _app(bogota)
    await _user(store, "editor@example.com", "admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        editor = await _signin(c, "editor@example.com")
        r = await c.put("/v1/admin/cities/bogota/config", headers=editor,
                        json={"fares": {"base": 3400}, "updatedBy": "somebody else"})
        assert r.json()["updatedBy"] == "editor@example.com"


async def test_city_scope_is_enforced_on_every_route_not_only_the_ui(bogota: City):
    app, store = _app(bogota, cities=("bogota", "medellin"))
    await _user(store, "bog@example.com", "admin", cities=["bogota"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = await _signin(c, "bog@example.com")
        assert (await c.get("/v1/admin/auth/me", headers=h)).json()["cities"] == ["bogota"]
        assert (await c.get("/v1/admin/cities/bogota/config", headers=h)).status_code == 200
        for method, path in (("get", "/v1/admin/cities/medellin/config"),
                             ("get", "/v1/admin/cities/medellin/config/history"),
                             ("post", "/v1/admin/cities/medellin/purge")):
            r = await getattr(c, method)(path, headers=h)
            assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", path
        r = await c.put("/v1/admin/cities/medellin/config", headers=h, json={"fares": {"base": 3400}})
        assert r.status_code == 403


async def test_owner_manages_accounts(bogota: City):
    app, store = _app(bogota, cities=("bogota", "medellin"))
    await _user(store, "owner@example.com", "owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        owner = await _signin(c, "owner@example.com")

        r = await c.post("/v1/admin/users", headers=owner,
                         json={"email": "New@Example.com", "password": PW, "name": "New",
                               "role": "admin", "cities": ["bogota"]})
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["email"] == "New@Example.com" and created["cities"] == ["bogota"]
        assert "password" not in r.text.lower() or "passwordHash" not in r.text

        # a duplicate (case-insensitive) and an unknown city are both refused
        assert (await c.post("/v1/admin/users", headers=owner,
                             json={"email": "new@example.com", "password": PW})).status_code == 409
        assert (await c.post("/v1/admin/users", headers=owner,
                             json={"email": "x@example.com", "password": PW,
                                   "cities": ["paris"]})).status_code == 422
        assert (await c.post("/v1/admin/users", headers=owner,
                             json={"email": "x@example.com", "password": "short"})).status_code == 422
        assert (await c.post("/v1/admin/users", headers=owner,
                             json={"email": "x@example.com", "password": PW,
                                   "role": "root"})).status_code == 422

        emails = [u["email"] for u in (await c.get("/v1/admin/users", headers=owner)).json()["users"]]
        assert emails == ["New@Example.com", "owner@example.com"]

        # the new account signs in, then widening its scope revokes the session it already had
        new = await _signin(c, "new@example.com")
        assert (await c.get("/v1/admin/cities/medellin/config", headers=new)).status_code == 403
        r = await c.patch(f"/v1/admin/users/{created['id']}", headers=owner, json={"cities": []})
        assert r.status_code == 200 and r.json()["cities"] == []
        assert (await c.get("/v1/admin/auth/me", headers=new)).status_code == 401
        new = await _signin(c, "new@example.com")
        assert (await c.get("/v1/admin/cities/medellin/config", headers=new)).status_code == 200

        # disabling ends the session and the password immediately
        r = await c.post(f"/v1/admin/users/{created['id']}/disable", headers=owner)
        assert r.status_code == 200 and r.json()["disabled"] is True
        assert (await c.get("/v1/admin/auth/me", headers=new)).status_code == 401
        assert (await c.post("/v1/admin/auth/login",
                             json={"email": "new@example.com", "password": PW})).status_code == 401

        assert (await c.patch("/v1/admin/users/999", headers=owner, json={"name": "x"})).status_code == 404


async def test_the_last_owner_cannot_lock_everyone_out(bogota: City):
    app, store = _app(bogota)
    owner_row = await _user(store, "owner@example.com", "owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        owner = await _signin(c, "owner@example.com")
        assert (await c.post(f"/v1/admin/users/{owner_row['id']}/disable", headers=owner)).status_code == 409
        assert (await c.patch(f"/v1/admin/users/{owner_row['id']}", headers=owner,
                              json={"role": "viewer"})).status_code == 409
        # with a second owner it is allowed again
        r = await c.post("/v1/admin/users", headers=owner,
                         json={"email": "two@example.com", "password": PW, "role": "owner"})
        assert r.status_code == 201
        assert (await c.patch(f"/v1/admin/users/{owner_row['id']}", headers=owner,
                              json={"role": "viewer"})).status_code == 200


# ------------------------------------------------------------------ machine credential
async def test_machine_credential_still_works_and_can_be_switched_off(bogota: City):
    app, _ = _app(bogota)
    cfg = settings()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/admin/auth/me", headers=MACHINE)
        assert r.status_code == 200 and r.json()["user"]["kind"] == "machine"
        assert r.json()["canManageUsers"] is False
        assert (await c.get("/v1/admin/cities/bogota/config", headers=MACHINE)).status_code == 200
        assert (await c.post("/v1/admin/cities/bogota/purge", headers=MACHINE)).status_code == 200
        assert (await c.get("/v1/admin/users", headers=MACHINE)).status_code == 403
        assert (await c.get("/v1/admin/auth/me", headers={"X-Admin-Token": "wrong"})).status_code == 401

        cfg.ADMIN_TOKEN_ENABLED = False
        try:
            r = await c.get("/v1/admin/auth/me", headers=MACHINE)
            assert r.status_code == 401 and "disabled" in r.json()["error"]["message"]
        finally:
            cfg.ADMIN_TOKEN_ENABLED = True


async def test_a_session_beats_the_machine_token_when_both_are_sent(bogota: City):
    app, store = _app(bogota)
    await _user(store, "viewer@example.com", "viewer")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        h = {**MACHINE, **await _signin(c, "viewer@example.com")}
        assert (await c.get("/v1/admin/auth/me", headers=h)).json()["user"]["role"] == "viewer"
        assert (await c.post("/v1/admin/cities/bogota/purge", headers=h)).status_code == 403


# ------------------------------------------------------------------ bootstrap
async def test_bootstrap_creates_the_first_owner_and_then_refuses():
    store = MemoryAdminUserStore()
    first = await bootstrap_owner(store, "First@Example.com", PW, "First")
    assert first is not None and first["role"] == "owner" and first["cities"] == []
    assert await bootstrap_owner(store, "second@example.com", PW) is None
    assert await store.count_users() == 1
    assert principal_from_user(first).label == "first@example.com"
