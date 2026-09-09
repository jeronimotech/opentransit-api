"""
v1.12 sign in with Google / Microsoft.

The interesting tests here are the failures. A happy path proves the wiring; what proves the design is
that a token with the wrong issuer, the wrong audience, an expired `exp`, a signature from a key we
never trusted, an unverified email or a replayed `state` all get the same answer: no.
"""
from __future__ import annotations

import base64
import datetime as dt

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admin_auth import (
    MemoryAdminUserStore,
    domain_provisioner,
    hash_password,
    hash_token,
    sign_in_with_identity,
)
from app.admin_config import MemoryConfigStore
from app.cities import City
from app.config import settings
from app.errors import ApiError, install_error_handlers
from app.oidc import (
    GOOGLE_DISCOVERY,
    Identity,
    MemoryOidcStateStore,
    OidcService,
    Provider,
    code_challenge,
    configured_providers,
    new_code_verifier,
    redirect_uri_for,
)
from app.routers import admin
from app.rt import RTCache
from app.runtime import CityRuntime

PW = "correct horse battery"
TID = "11111111-2222-3333-4444-555555555555"
OTHER_TID = "99999999-9999-9999-9999-999999999999"
WEB = "https://bogota.opentransit.tech"

GOOGLE_P = Provider("google", "Google", "google-client-id", "google-secret", GOOGLE_DISCOVERY,
                    "openid email profile")
MICROSOFT_P = Provider("microsoft", "Microsoft", "ms-client-id", "ms-secret",
                       f"https://login.microsoftonline.com/{TID}/v2.0/.well-known/openid-configuration",
                       "openid email profile", frozenset({TID}))


# ------------------------------------------------------------------ a provider we control
def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY, WRONG_KEY = _rsa(), _rsa()
KID = "test-key-1"


def _b64u(i: int) -> str:
    return base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


def _jwk(key, kid: str = KID) -> dict:
    n = key.public_key().public_numbers()
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": _b64u(n.n), "e": _b64u(n.e)}


GOOGLE_DOC = {"issuer": "https://accounts.google.com",
              "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
              "token_endpoint": "https://oauth2.googleapis.com/token",
              "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs"}
# The multi-tenant form of the document, verbatim: Entra answers `common` with the *template*, which is
# why the issuer has to be resolved per token.
MS_DOC = {"issuer": "https://login.microsoftonline.com/{tenantid}/v2.0",
          "authorization_endpoint": f"https://login.microsoftonline.com/{TID}/oauth2/v2.0/authorize",
          "token_endpoint": f"https://login.microsoftonline.com/{TID}/oauth2/v2.0/token",
          "jwks_uri": f"https://login.microsoftonline.com/{TID}/discovery/v2.0/keys"}


class FakeFetcher:
    """The two providers, offline. Nothing in the service is allowed to reach the network in tests."""

    def __init__(self) -> None:
        self.keys = [_jwk(KEY)]
        self.id_token = ""
        self.token_response: dict | None = None
        self.exchanges: list[dict] = []
        self.jwks_fetches = 0

    async def get_json(self, url: str) -> dict:
        if url == GOOGLE_DISCOVERY:
            return dict(GOOGLE_DOC)
        if url.endswith("/.well-known/openid-configuration"):
            return dict(MS_DOC)
        if "certs" in url or url.endswith("/keys"):
            self.jwks_fetches += 1
            return {"keys": self.keys}
        raise AssertionError(f"unexpected fetch: {url}")

    async def post_form(self, url: str, data: dict) -> dict:
        self.exchanges.append(dict(data))
        return self.token_response if self.token_response is not None else {"id_token": self.id_token}


def _token(claims: dict, *, key=KEY, kid: str = KID, alg: str = "RS256") -> str:
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def _google_claims(**over) -> dict:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    return {"iss": "https://accounts.google.com", "aud": GOOGLE_P.client_id, "sub": "google-sub-1",
            "email": "luis@example.com", "email_verified": True, "name": "Luis",
            "iat": now, "exp": now + 600, "nonce": "N", **over}


def _ms_claims(**over) -> dict:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    return {"iss": f"https://login.microsoftonline.com/{TID}/v2.0", "aud": MICROSOFT_P.client_id,
            "sub": "ms-pairwise-sub", "oid": "ms-object-id", "tid": TID, "email": "luis@example.com",
            "name": "Luis", "iat": now, "exp": now + 600, "nonce": "N", **over}


def _service(*providers: Provider) -> tuple[OidcService, FakeFetcher]:
    f = FakeFetcher()
    return OidcService({p.id: p for p in providers}, f), f


async def _verify(svc: OidcService, p: Provider, claims: dict, *, nonce: str = "N", **kw) -> Identity:
    return await svc.identity(p, _token(claims, **kw), nonce=nonce)


# ------------------------------------------------------------------ configuration
def test_a_provider_without_credentials_is_simply_absent():
    class Cfg:
        GOOGLE_CLIENT_ID = GOOGLE_CLIENT_SECRET = None
        MICROSOFT_CLIENT_ID = MICROSOFT_CLIENT_SECRET = None
        MICROSOFT_TENANT = MICROSOFT_ALLOWED_TENANT_IDS = ""

    assert configured_providers(Cfg()) == {}
    Cfg.GOOGLE_CLIENT_ID = "id"                      # half-configured stays off
    assert configured_providers(Cfg()) == {}
    Cfg.GOOGLE_CLIENT_SECRET = "secret"
    assert set(configured_providers(Cfg())) == {"google"}


def test_microsoft_refuses_to_enable_without_an_explicit_tenant():
    class Cfg:
        GOOGLE_CLIENT_ID = GOOGLE_CLIENT_SECRET = None
        MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET = "id", "secret"
        MICROSOFT_TENANT = ""
        MICROSOFT_ALLOWED_TENANT_IDS = ""

    assert configured_providers(Cfg()) == {}          # no tenant at all
    Cfg.MICROSOFT_TENANT = "common"
    # `common` means every Entra tenant on earth; without an allowlist that is a wide-open front door.
    assert configured_providers(Cfg()) == {}
    Cfg.MICROSOFT_ALLOWED_TENANT_IDS = f" {TID} ,"
    assert configured_providers(Cfg())["microsoft"].tenants == frozenset({TID})
    Cfg.MICROSOFT_TENANT, Cfg.MICROSOFT_ALLOWED_TENANT_IDS = TID.upper(), ""
    assert configured_providers(Cfg())["microsoft"].tenants == frozenset({TID})


def test_the_redirect_uri_is_ours_and_https():
    class Cfg:
        OIDC_REDIRECT_BASE = "https://bogota.opentransit.tech/"
        WEB_BASE_URL = None
        OIDC_ALLOW_INSECURE_HTTP = False

    assert redirect_uri_for(Cfg(), "google") == f"{WEB}/admin/auth/callback/google"
    Cfg.OIDC_REDIRECT_BASE = "http://evil.example"
    with pytest.raises(ApiError) as e:
        redirect_uri_for(Cfg(), "google")
    assert e.value.status == 503
    Cfg.OIDC_ALLOW_INSECURE_HTTP = True               # localhost development, and only that
    assert redirect_uri_for(Cfg(), "google").startswith("http://evil.example/")
    Cfg.OIDC_REDIRECT_BASE, Cfg.OIDC_ALLOW_INSECURE_HTTP = None, False
    with pytest.raises(ApiError):
        redirect_uri_for(Cfg(), "google")


# ------------------------------------------------------------------ PKCE and the authorization URL
def test_pkce_challenge_is_the_sha256_of_the_verifier():
    v = new_code_verifier()
    assert 43 <= len(v) <= 128
    # RFC 7636 S256: base64url(sha256(verifier)), unpadded
    assert code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert "=" not in code_challenge(v)


async def test_start_hides_the_verifier_and_asks_for_S256():
    svc, _ = _service(GOOGLE_P)
    states = MemoryOidcStateStore()
    out = await svc.start(states, "google", redirect_uri=f"{WEB}/admin/auth/callback/google", ttl_s=600)

    url = out["authorizationUrl"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "code_challenge_method=S256" in url and "response_type=code" in url
    assert f"state={out['state']}" in url and "nonce=" in url
    # The verifier itself never leaves the server, and neither secret is stored in the clear.
    row = next(iter(states.states.values()))
    assert row["code_verifier"] not in url
    assert code_challenge(row["code_verifier"]) in url
    assert out["state"] not in states.states and hash_token(out["state"]) in states.states
    assert row["browser_hash"] == hash_token(out["browserToken"])
    assert out["browserToken"] not in url


# ------------------------------------------------------------------ id_token validation
async def test_a_good_google_token_yields_the_subject_and_the_verified_email():
    svc, _ = _service(GOOGLE_P)
    ident = await _verify(svc, GOOGLE_P, _google_claims())
    assert ident == Identity("google", "google-sub-1", "luis@example.com", "Luis")
    # the address is the lookup, never the identity: casing is normalised away
    assert (await _verify(svc, GOOGLE_P, _google_claims(email="LUIS@Example.com"))).email == "luis@example.com"


@pytest.mark.parametrize("claims, why", [
    ({"iss": "https://accounts.evil.example"}, "issuer"),
    ({"iss": "https://login.microsoftonline.com/x/v2.0"}, "issuer"),
    ({"aud": "some-other-app.apps.googleusercontent.com"}, "application"),
    ({"exp": int(dt.datetime.now(dt.UTC).timestamp()) - 3600,
      "iat": int(dt.datetime.now(dt.UTC).timestamp()) - 7200}, "expired"),
    ({"nonce": "a-different-sign-in"}, "nonce"),
    ({"email": ""}, "email address"),
    ({"email_verified": False}, "not verified"),
    ({"email_verified": None}, "not verified"),
])
async def test_google_id_tokens_that_must_be_refused(claims, why):
    svc, _ = _service(GOOGLE_P)
    with pytest.raises(ApiError) as e:
        await _verify(svc, GOOGLE_P, _google_claims(**claims))
    assert e.value.status == 401 and why in e.value.message


async def test_a_signature_from_a_key_the_provider_never_published_is_refused():
    svc, _ = _service(GOOGLE_P)
    # same kid, different key: the forgery that a naive "decode and read the claims" would wave through
    with pytest.raises(ApiError) as e:
        await _verify(svc, GOOGLE_P, _google_claims(), key=WRONG_KEY)
    assert e.value.status == 401 and "signature" in e.value.message
    # an unknown kid never even reaches the verifier
    with pytest.raises(ApiError) as e:
        await _verify(svc, GOOGLE_P, _google_claims(), kid="not-a-kid")
    assert "does not publish" in e.value.message


async def test_unsigned_and_symmetric_tokens_are_refused_before_anything_else():
    svc, _ = _service(GOOGLE_P)
    for token in (jwt.encode(_google_claims(), None, algorithm="none", headers={"kid": KID}),
                  jwt.encode(_google_claims(), "public-key-as-a-password", algorithm="HS256",
                             headers={"kid": KID})):
        with pytest.raises(ApiError) as e:
            await svc.identity(GOOGLE_P, token, nonce="N")
        assert e.value.status == 401 and "algorithm" in e.value.message


async def test_a_missing_required_claim_is_refused():
    svc, _ = _service(GOOGLE_P)
    claims = _google_claims()
    del claims["sub"]
    with pytest.raises(ApiError) as e:
        await _verify(svc, GOOGLE_P, claims)
    assert e.value.status == 401


async def test_a_key_rollover_is_picked_up_once_not_on_every_forgery():
    svc, f = _service(GOOGLE_P)
    assert await _verify(svc, GOOGLE_P, _google_claims())
    fetches = f.jwks_fetches
    f.keys = [_jwk(KEY, "rolled-over")]
    assert await svc.identity(GOOGLE_P, _token(_google_claims(), kid="rolled-over"), nonce="N")
    assert f.jwks_fetches == fetches + 1
    # a second unknown kid straight away does *not* buy another fetch of the provider's keys
    with pytest.raises(ApiError):
        await svc.identity(GOOGLE_P, _token(_google_claims(), kid="also-unknown"), nonce="N")
    assert f.jwks_fetches == fetches + 1


# ------------------------------------------------------------------ Microsoft's extra rules
async def test_microsoft_identifies_by_the_object_id_and_resolves_the_templated_issuer():
    svc, _ = _service(MICROSOFT_P)
    ident = await _verify(svc, MICROSOFT_P, _ms_claims())
    # `oid` survives the app registration being replaced; `sub` is pairwise and would not
    assert ident == Identity("microsoft", "ms-object-id", "luis@example.com", "Luis")
    fallback = await _verify(svc, MICROSOFT_P, {k: v for k, v in _ms_claims().items() if k != "oid"})
    assert fallback.subject == "ms-pairwise-sub"


async def test_a_stranger_tenant_cannot_sign_in_even_with_a_perfectly_valid_token():
    """Entra signs every tenant with the same keys, so 'valid signature' says nothing about *who*."""
    svc, _ = _service(MICROSOFT_P)
    hostile = _ms_claims(tid=OTHER_TID, iss=f"https://login.microsoftonline.com/{OTHER_TID}/v2.0",
                         email="luis@example.com")
    with pytest.raises(ApiError) as e:
        await _verify(svc, MICROSOFT_P, hostile)
    assert e.value.status == 401 and "tenant" in e.value.message


async def test_microsoft_issuer_must_match_the_tenant_it_claims():
    svc, _ = _service(MICROSOFT_P)
    with pytest.raises(ApiError) as e:
        await _verify(svc, MICROSOFT_P, _ms_claims(iss=f"https://login.microsoftonline.com/{OTHER_TID}/v2.0"))
    assert "issuer" in e.value.message


async def test_microsoft_email_marked_unverified_by_the_domain_owner_is_refused():
    svc, _ = _service(MICROSOFT_P)
    assert (await _verify(svc, MICROSOFT_P, _ms_claims(xms_edov=True))).email == "luis@example.com"
    with pytest.raises(ApiError) as e:
        await _verify(svc, MICROSOFT_P, _ms_claims(xms_edov=False))
    assert e.value.status == 401 and "not verified" in e.value.message
    with pytest.raises(ApiError):
        await _verify(svc, MICROSOFT_P, _ms_claims(email=""))


# ------------------------------------------------------------------ state: single use, browser-bound
async def test_state_is_single_use_short_lived_and_bound_to_one_browser():
    svc, f = _service(GOOGLE_P)
    states = MemoryOidcStateStore()
    uri = f"{WEB}/admin/auth/callback/google"

    async def start() -> dict:
        return await svc.start(states, "google", redirect_uri=uri, ttl_s=600)

    out = await start()
    f.id_token = _token(_google_claims(nonce=states.states[hash_token(out["state"])]["nonce"]))
    ident = await svc.complete(states, "google", state=out["state"], code="auth-code",
                               browser_token=out["browserToken"], redirect_uri=uri)
    assert ident.email == "luis@example.com"
    # the exchange carried the verifier and the secret, and nothing else was invented
    assert f.exchanges[-1]["grant_type"] == "authorization_code"
    assert f.exchanges[-1]["code_verifier"] and f.exchanges[-1]["client_secret"] == "google-secret"

    # replay: the row is gone, so the same state cannot be spent twice
    with pytest.raises(ApiError) as e:
        await svc.complete(states, "google", state=out["state"], code="auth-code",
                           browser_token=out["browserToken"], redirect_uri=uri)
    assert e.value.status == 401 and "already used" in e.value.message

    # another browser holding a stolen `state` cannot finish the flow
    out = await start()
    f.id_token = _token(_google_claims(nonce=states.states[hash_token(out["state"])]["nonce"]))
    with pytest.raises(ApiError) as e:
        await svc.complete(states, "google", state=out["state"], code="c",
                           browser_token="a-token-from-elsewhere", redirect_uri=uri)
    assert "different browser" in e.value.message

    # a made-up state, and one that has expired, look identical from outside
    with pytest.raises(ApiError):
        await svc.complete(states, "google", state="invented", code="c", browser_token="x",
                           redirect_uri=uri)
    out = await start()
    states.states[hash_token(out["state"])]["expires_at"] = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    with pytest.raises(ApiError):
        await svc.complete(states, "google", state=out["state"], code="c",
                           browser_token=out["browserToken"], redirect_uri=uri)
    assert await states.drop_expired_states() == 0     # take_state already removed it


async def test_a_flow_started_for_one_provider_cannot_be_finished_at_another():
    svc, f = _service(GOOGLE_P, MICROSOFT_P)
    states = MemoryOidcStateStore()
    uri = f"{WEB}/admin/auth/callback/google"
    out = await svc.start(states, "google", redirect_uri=uri, ttl_s=600)
    f.id_token = _token(_ms_claims())
    with pytest.raises(ApiError) as e:
        await svc.complete(states, "microsoft", state=out["state"], code="c",
                           browser_token=out["browserToken"],
                           redirect_uri=f"{WEB}/admin/auth/callback/microsoft")
    assert "different provider" in e.value.message


async def test_a_redirect_uri_that_changed_mid_flow_is_refused():
    svc, f = _service(GOOGLE_P)
    states = MemoryOidcStateStore()
    out = await svc.start(states, "google", redirect_uri=f"{WEB}/admin/auth/callback/google", ttl_s=600)
    f.id_token = _token(_google_claims())
    with pytest.raises(ApiError) as e:
        await svc.complete(states, "google", state=out["state"], code="c",
                           browser_token=out["browserToken"],
                           redirect_uri="https://evil.example/admin/auth/callback/google")
    assert "different address" in e.value.message


# ------------------------------------------------------------------ who is allowed in
async def test_a_verified_google_identity_does_not_create_an_account():
    store = MemoryAdminUserStore()
    ident = Identity("google", "sub-1", "stranger@example.com", "Stranger")
    with pytest.raises(ApiError) as e:
        await sign_in_with_identity(store, ident)
    assert e.value.status == 403 and "invite" in e.value.message
    assert await store.count_users() == 0             # the whole point: no account was created


async def test_a_disabled_account_cannot_be_re_entered_through_a_provider():
    store = MemoryAdminUserStore()
    user = await store.create_user(email="luis@example.com", password_hash=hash_password(PW),
                                   name="Luis", role="admin", cities=[])
    ident = Identity("google", "sub-1", "luis@example.com", "Luis")
    assert (await sign_in_with_identity(store, ident))["id"] == user["id"]
    await store.update_user(user["id"], disabled=True)
    with pytest.raises(ApiError) as e:
        await sign_in_with_identity(store, ident)
    assert e.value.status == 403


async def test_the_subject_is_what_binds_and_a_mismatch_is_refused_both_ways():
    store = MemoryAdminUserStore()
    luis = await store.create_user(email="luis@example.com", password_hash=hash_password(PW), name="Luis",
                                   role="owner", cities=[])
    ana = await store.create_user(email="ana@example.com", password_hash=hash_password(PW), name="Ana",
                                  role="admin", cities=[])
    await sign_in_with_identity(store, Identity("google", "sub-luis", "luis@example.com", "Luis"))
    assert (await store.identity("google", "sub-luis"))["user_id"] == luis["id"]

    # somebody who now controls the address, but not the Google account behind it
    with pytest.raises(ApiError) as e:
        await sign_in_with_identity(store, Identity("google", "sub-somebody-else", "luis@example.com", ""))
    assert e.value.status == 403 and "different" in e.value.message

    # and the other direction: the linked Google account trying to become a different admin
    with pytest.raises(ApiError) as e:
        await sign_in_with_identity(store, Identity("google", "sub-luis", "ana@example.com", ""))
    assert e.value.status == 403

    # a different provider is a separate link, and unlinking lets the next sign-in re-link
    await sign_in_with_identity(store, Identity("microsoft", "oid-luis", "luis@example.com", "Luis"))
    assert {i["provider"] for i in await store.identities_of(luis["id"])} == {"google", "microsoft"}
    assert await store.unlink_identity(luis["id"], "google") is True
    await sign_in_with_identity(store, Identity("google", "sub-new-key", "luis@example.com", "Luis"))
    assert (await store.identity("google", "sub-new-key"))["user_id"] == luis["id"]
    assert await store.identities_of(ana["id"]) == []


async def test_domain_provisioning_is_off_unless_asked_for_and_then_narrow():
    store = MemoryAdminUserStore()
    assert domain_provisioner(store, domains=[], role="viewer", cities=[]) is None
    assert domain_provisioner(store, domains=["  "], role="viewer", cities=[]) is None

    provision = domain_provisioner(store, domains=["@example.com"], role="viewer", cities=["bogota"])
    outside = Identity("google", "s1", "someone@other.example", "")
    with pytest.raises(ApiError):
        await sign_in_with_identity(store, outside, provision=provision)
    assert await store.count_users() == 0

    inside = Identity("google", "s2", "New@example.com", "New")
    user = await sign_in_with_identity(store, inside, provision=provision)
    assert user["role"] == "viewer" and user["cities"] == ["bogota"] and not user["disabled"]
    # no password anybody could know, so the account exists only behind the provider
    assert user["password_hash"].startswith("$argon2id$")


# ------------------------------------------------------------------ the endpoints
def _app(bogota: City, svc=None) -> tuple[FastAPI, MemoryAdminUserStore, MemoryOidcStateStore]:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(admin.router)
    app.state.cities = {"bogota": CityRuntime(city=bogota, rt=RTCache(bogota), otp=None)}  # type: ignore[arg-type]
    app.state.config_store = MemoryConfigStore()
    store, states = MemoryAdminUserStore(), MemoryOidcStateStore()
    app.state.admin_users, app.state.oidc_states = store, states
    if svc is not None:
        app.state.oidc = svc
    return app, store, states


async def test_an_unconfigured_provider_is_absent_and_its_endpoints_refuse(bogota: City):
    app, _, _ = _app(bogota)                      # no app.state.oidc at all
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/admin/auth/providers")
        assert r.status_code == 200 and r.json() == {"password": True, "providers": []}
        for path in ("/v1/admin/auth/oidc/google/start", "/v1/admin/auth/oidc/microsoft/start"):
            assert (await c.post(path)).status_code == 404
        r = await c.post("/v1/admin/auth/oidc/google/callback",
                         json={"state": "s", "code": "c", "browserToken": "b"})
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"

    # Google on, Microsoft off: only the configured one is offered, and only it answers.
    svc, _ = _service(GOOGLE_P)
    app, _, _ = _app(bogota, svc)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/v1/admin/auth/providers")).json()["providers"] == \
            [{"id": "google", "label": "Google"}]
        r = await c.post("/v1/admin/auth/oidc/microsoft/start")
        assert r.status_code == 404 and "not configured" in r.json()["error"]["message"]


@pytest.fixture
def redirect_base():
    cfg = settings()
    before = cfg.OIDC_REDIRECT_BASE
    cfg.OIDC_REDIRECT_BASE = WEB
    yield
    cfg.OIDC_REDIRECT_BASE = before


async def test_the_round_trip_issues_the_same_session_a_password_would(bogota: City, redirect_base):
    svc, f = _service(GOOGLE_P)
    app, store, states = _app(bogota, svc)
    await store.create_user(email="luis@example.com", password_hash=hash_password(PW), name="Luis",
                            role="admin", cities=["bogota"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        start = (await c.post("/v1/admin/auth/oidc/google/start")).json()
        assert start["authorizationUrl"].startswith("https://accounts.google.com/")
        assert f"redirect_uri={WEB.replace(':', '%3A').replace('/', '%2F')}" in start["authorizationUrl"]

        nonce = states.states[hash_token(start["state"])]["nonce"]
        f.id_token = _token(_google_claims(nonce=nonce))
        r = await c.post("/v1/admin/auth/oidc/google/callback",
                         json={"state": start["state"], "code": "an-authorization-code",
                               "browserToken": start["browserToken"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["email"] == "luis@example.com" and body["cities"] == ["bogota"]
        assert hash_token(body["token"]) in store.sessions

        # …and that session is an ordinary one: same roles, same scope, same revocation
        h = {"Authorization": f"Bearer {body['token']}"}
        assert (await c.get("/v1/admin/auth/me", headers=h)).json()["user"]["role"] == "admin"
        assert (await c.get("/v1/admin/users", headers=h)).status_code == 403
        assert (await c.post("/v1/admin/auth/logout", headers=h)).status_code == 200
        assert (await c.get("/v1/admin/auth/me", headers=h)).status_code == 401

        # nothing that could be replayed is left behind
        assert states.states == {}


async def test_the_callback_refuses_an_address_with_no_account(bogota: City, redirect_base):
    svc, f = _service(GOOGLE_P)
    app, store, states = _app(bogota, svc)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        start = (await c.post("/v1/admin/auth/oidc/google/start")).json()
        f.id_token = _token(_google_claims(nonce=states.states[hash_token(start["state"])]["nonce"],
                                           email="stranger@gmail.com"))
        r = await c.post("/v1/admin/auth/oidc/google/callback",
                         json={"state": start["state"], "code": "c", "browserToken": start["browserToken"]})
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
        assert await store.count_users() == 0 and store.sessions == {}


async def test_starting_a_sign_in_is_rate_limited(bogota: City, redirect_base):
    svc, _ = _service(GOOGLE_P)
    app, _, _ = _app(bogota, svc)
    admin._oidc_throttle._fails.clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            codes = {(await c.post("/v1/admin/auth/oidc/google/start")).status_code for _ in range(31)}
        assert codes == {200, 429}
    finally:
        admin._oidc_throttle._fails.clear()


async def test_a_deployment_without_a_redirect_base_says_so_instead_of_guessing(bogota: City):
    svc, _ = _service(GOOGLE_P)
    app, _, _ = _app(bogota, svc)
    cfg = settings()
    before = cfg.OIDC_REDIRECT_BASE, cfg.WEB_BASE_URL
    cfg.OIDC_REDIRECT_BASE, cfg.WEB_BASE_URL = None, None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/v1/admin/auth/oidc/google/start")
            assert r.status_code == 503 and r.json()["error"]["code"] == "UNAVAILABLE"
    finally:
        cfg.OIDC_REDIRECT_BASE, cfg.WEB_BASE_URL = before
