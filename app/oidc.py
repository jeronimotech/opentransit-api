"""
Sign in with Google or Microsoft (OpenID Connect, authorization code + PKCE).

The decision this file exists to enforce: **a provider proves who you are, it never decides whether you
may come in.** A successful Google or Microsoft sign-in never creates an account. It looks up an
existing, enabled `admin_user` by email; if there is none, it is a 403. An owner invites somebody
first — `scripts/admin_user.py create`, or the accounts screen. Auto-provisioning "whoever can prove
they own an address at some domain" is how admin panels get taken over, so it is off, per deployment,
and documented in SECURITY.md as the risk it is.

Three more rules the rest of the code leans on:

* **the subject, not the string.** An email address is a display name that happens to be unique today.
  The first successful sign-in links the provider's stable subject id to the account; later sign-ins
  must present the same subject, and a mismatch is refused rather than silently re-linked. Microsoft
  says as much itself: "never use `email` for authorization" — mutable, and in a tenant somebody else
  administers, settable.
* **`state` and the PKCE verifier are single-use, short-lived and bound to the browser that started
  the flow.** They live in Postgres (`admin_oidc_state`), are taken with a DELETE ... RETURNING so a
  replay finds nothing, expire in `OIDC_STATE_TTL_SECONDS`, and carry the digest of a second secret
  that only the starting browser holds in an httpOnly cookie.
* **the id_token is verified, not read.** Signature against the provider's JWKS (RS256 only — never
  `none`, never a symmetric algorithm), issuer, audience, expiry, nonce, and the provider's own
  statement that the email is verified. Everything else in the token is decoration.

Nothing here logs a client secret, an authorization code, a token or an id_token.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from .admin_auth import EMAIL_RE, hash_token, normalize_email, token_matches
from .db import pool
from .errors import ApiError, Unauthorized

log = logging.getLogger("ot.oidc")

GOOGLE = "google"
MICROSOFT = "microsoft"

GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
# Verified against the live discovery document: `issuer` is "https://accounts.google.com", and Google's
# own guidance also accepts the bare host form in tokens.
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})

# Verified against https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration:
# a multi-tenant authority answers with the *template* "https://login.microsoftonline.com/{tenantid}/v2.0",
# which is why the tenant id has to be resolved per token instead of compared to a constant.
MICROSOFT_AUTHORITY = "https://login.microsoftonline.com/{tenant}/v2.0"

# Only asymmetric RS256 — both providers advertise exactly that in `id_token_signing_alg_values_supported`.
# Pinning it is what stops the classic "alg: none" and "alg: HS256 signed with the public key" forgeries.
ALLOWED_ALGS = ("RS256",)
CLOCK_SKEW_S = 60
DISCOVERY_TTL_S = 3600
JWKS_TTL_S = 3600
JWKS_MIN_REFETCH_S = 60          # an unknown kid may mean a key rollover; it may also mean a flood

_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _truthy(v: Any) -> bool:
    """Providers send JSON `true`; a few gateways re-encode it as the string "true". Nothing else counts."""
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


# ------------------------------------------------------------------ configuration
@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    client_id: str
    client_secret: str
    discovery_url: str
    scopes: str
    # Microsoft only: the `tid` values allowed to sign in. Never empty for a configured provider — see
    # `configured_providers`, which refuses to enable Microsoft without one.
    tenants: frozenset[str] = frozenset()


def _split(csv: str | None) -> list[str]:
    return [s.strip() for s in (csv or "").split(",") if s.strip()]


def configured_providers(cfg: Any) -> dict[str, Provider]:
    """
    Only providers whose credentials are actually present. A half-configured provider is left *off* and
    logged, never enabled with a default: the login screen offers a button only for what is here, and
    every endpoint 404s for what is not.
    """
    out: dict[str, Provider] = {}

    gid, gsecret = (cfg.GOOGLE_CLIENT_ID or "").strip(), (cfg.GOOGLE_CLIENT_SECRET or "").strip()
    if gid and gsecret:
        out[GOOGLE] = Provider(GOOGLE, "Google", gid, gsecret, GOOGLE_DISCOVERY, "openid email profile")
    elif gid or gsecret:
        log.warning("Google sign-in stays off: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must both be set")

    mid, msecret = (cfg.MICROSOFT_CLIENT_ID or "").strip(), (cfg.MICROSOFT_CLIENT_SECRET or "").strip()
    tenant = (cfg.MICROSOFT_TENANT or "").strip()
    if mid and msecret:
        if not tenant:
            log.warning("Microsoft sign-in stays off: MICROSOFT_TENANT is required (your tenant GUID)")
        else:
            tenants = {t.lower() for t in _split(cfg.MICROSOFT_ALLOWED_TENANT_IDS)}
            if _GUID_RE.match(tenant):
                tenants.add(tenant.lower())
            if not tenants:
                # `common`/`organizations`/`consumers` (or a domain we cannot resolve to a GUID here) would
                # otherwise let *any* Entra tenant on earth mint tokens for us. Refuse rather than guess.
                log.warning("Microsoft sign-in stays off: MICROSOFT_TENANT=%s needs "
                            "MICROSOFT_ALLOWED_TENANT_IDS (the tenant GUIDs allowed to sign in)", tenant)
            else:
                out[MICROSOFT] = Provider(
                    MICROSOFT, "Microsoft", mid, msecret,
                    MICROSOFT_AUTHORITY.format(tenant=tenant) + "/.well-known/openid-configuration",
                    "openid email profile", frozenset(tenants))
    elif mid or msecret:
        log.warning("Microsoft sign-in stays off: MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET "
                    "must both be set")
    return out


def redirect_uri_for(cfg: Any, provider: str) -> str:
    """
    The one address the provider is allowed to send the browser back to, computed here rather than
    accepted from the client: a `redirect_uri` a caller can choose is an open redirect with an
    authorization code attached. It must also be the exact string registered in the provider's console.
    """
    base = ((cfg.OIDC_REDIRECT_BASE or cfg.WEB_BASE_URL) or "").strip().rstrip("/")
    if not base:
        raise ApiError("provider sign-in needs OIDC_REDIRECT_BASE (the web client's public URL)",
                       status=503, code="UNAVAILABLE")
    if not base.startswith("https://") and not cfg.OIDC_ALLOW_INSECURE_HTTP:
        raise ApiError("OIDC_REDIRECT_BASE must be https (set OIDC_ALLOW_INSECURE_HTTP=1 for localhost)",
                       status=503, code="UNAVAILABLE")
    return f"{base}/admin/auth/callback/{provider}"


# ------------------------------------------------------------------ PKCE
def new_code_verifier() -> str:
    """RFC 7636: 43-128 characters from the unreserved set. 43 bytes of entropy, url-safe."""
    return secrets.token_urlsafe(64)[:96]


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ------------------------------------------------------------------ state storage
class OidcStateStore(Protocol):
    async def create_state(self, *, state_hash: str, browser_hash: str, provider: str, code_verifier: str,
                           nonce: str, redirect_uri: str, expires_at: dt.datetime) -> None: ...
    async def take_state(self, state_hash: str, now: dt.datetime) -> dict | None: ...
    async def drop_expired_states(self) -> int: ...


_STATE_COLS = "state_hash, browser_hash, provider, code_verifier, nonce, redirect_uri, expires_at"


class PgOidcStateStore:
    async def create_state(self, *, state_hash: str, browser_hash: str, provider: str, code_verifier: str,
                           nonce: str, redirect_uri: str, expires_at: dt.datetime) -> None:
        async with pool().acquire() as c:
            await c.execute(
                f"INSERT INTO admin_oidc_state ({_STATE_COLS}) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                state_hash, browser_hash, provider, code_verifier, nonce, redirect_uri, expires_at)

    async def take_state(self, state_hash: str, now: dt.datetime) -> dict | None:
        """
        DELETE ... RETURNING, so "used once" is decided by Postgres rather than by a read-then-write race:
        two browsers replaying the same `state` can never both win.
        """
        async with pool().acquire() as c:
            r = await c.fetchrow(
                f"DELETE FROM admin_oidc_state WHERE state_hash=$1 RETURNING {_STATE_COLS}", state_hash)
        if r is None:
            return None
        row = dict(r)
        return None if row["expires_at"] <= now else row

    async def drop_expired_states(self) -> int:
        async with pool().acquire() as c:
            r = await c.execute("DELETE FROM admin_oidc_state WHERE expires_at <= now()")
        return int(r.rsplit(" ", 1)[-1] or 0)


class MemoryOidcStateStore:
    """Test double with the same contract as the Postgres store."""

    def __init__(self) -> None:
        self.states: dict[str, dict] = {}

    async def create_state(self, *, state_hash: str, browser_hash: str, provider: str, code_verifier: str,
                           nonce: str, redirect_uri: str, expires_at: dt.datetime) -> None:
        self.states[state_hash] = {"state_hash": state_hash, "browser_hash": browser_hash,
                                   "provider": provider, "code_verifier": code_verifier, "nonce": nonce,
                                   "redirect_uri": redirect_uri, "expires_at": expires_at}

    async def take_state(self, state_hash: str, now: dt.datetime) -> dict | None:
        row = self.states.pop(state_hash, None)
        if row is None:
            return None
        return None if row["expires_at"] <= now else copy.deepcopy(row)

    async def drop_expired_states(self) -> int:
        now, before = _now(), len(self.states)
        self.states = {k: v for k, v in self.states.items() if v["expires_at"] > now}
        return before - len(self.states)


# ------------------------------------------------------------------ network
class Fetcher(Protocol):
    async def get_json(self, url: str) -> dict: ...
    async def post_form(self, url: str, data: dict[str, str]) -> dict: ...


class HttpxFetcher:
    def __init__(self, timeout_s: float = 10.0) -> None:
        self.timeout_s = timeout_s

    async def get_json(self, url: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await c.get(url, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()

    async def post_form(self, url: str, data: dict[str, str]) -> dict:
        """
        The token exchange. `data` carries the client secret and the authorization code, so neither the
        request nor the response body is ever logged — only the provider's short `error` code, which is
        the one part of a failure that is safe and worth having.
        """
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            r = await c.post(url, data=data, headers={"Accept": "application/json"})
        if r.status_code >= 400:
            code = ""
            try:
                code = str(r.json().get("error") or "")[:60]
            except ValueError:
                pass
            log.warning("token exchange refused by the provider (HTTP %s%s)", r.status_code,
                        f", {code}" if code else "")
            raise Unauthorized("the identity provider refused this sign-in")
        return r.json()


# ------------------------------------------------------------------ the flow
@dataclass(frozen=True)
class Identity:
    """What a verified id_token is allowed to tell us. Nothing here is trusted for authorisation."""

    provider: str
    subject: str
    email: str
    name: str = ""


class OidcService:
    def __init__(self, providers: dict[str, Provider], fetcher: Fetcher | None = None) -> None:
        self.providers = providers
        self.fetcher = fetcher or HttpxFetcher()
        self._discovery: dict[str, tuple[float, dict]] = {}
        self._jwks: dict[str, tuple[float, dict[str, dict]]] = {}
        self._jwks_miss: dict[str, float] = {}

    # -------------------------------------------------------------- provider lookup
    def public(self) -> list[dict]:
        """What the login screen may know: a button per provider that is genuinely configured."""
        return [{"id": p.id, "label": p.label} for p in self.providers.values()]

    def provider(self, provider_id: str) -> Provider:
        p = self.providers.get(provider_id)
        if p is None:
            raise ApiError(f"{provider_id}: sign-in with this provider is not configured here",
                           status=404, code="NOT_FOUND")
        return p

    # -------------------------------------------------------------- discovery / keys
    async def metadata(self, p: Provider) -> dict:
        """The provider's own discovery document, cached for an hour. Endpoints move; memory doesn't."""
        hit = self._discovery.get(p.id)
        if hit and time.monotonic() - hit[0] < DISCOVERY_TTL_S:
            return hit[1]
        doc = await self.fetcher.get_json(p.discovery_url)
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
            if not isinstance(doc.get(field), str):
                raise ApiError(f"{p.id}: its discovery document has no {field}", status=502,
                               code="OIDC_ERROR")
        self._discovery[p.id] = (time.monotonic(), doc)
        return doc

    async def _fetch_jwks(self, p: Provider, jwks_uri: str) -> dict[str, dict]:
        doc = await self.fetcher.get_json(jwks_uri)
        keys = {str(k.get("kid")): k for k in (doc.get("keys") or []) if k.get("kid")}
        self._jwks[p.id] = (time.monotonic(), keys)
        return keys

    async def _signing_key(self, p: Provider, kid: str | None):
        """
        The public key for this `kid`. An unknown kid usually means a routine key rollover, so the first
        miss refetches immediately — sign-in must not break for an hour because a provider rotated a key.
        Further misses are ignored for JWKS_MIN_REFETCH_S, so a stream of forged kids cannot turn our own
        key fetch into a denial of service against the provider.
        """
        jwks_uri = (await self.metadata(p))["jwks_uri"]
        age, keys = self._jwks.get(p.id, (-1e9, {}))
        now = time.monotonic()
        if now - age >= JWKS_TTL_S:
            keys = await self._fetch_jwks(p, jwks_uri)
        elif kid and kid not in keys and now - self._jwks_miss.get(p.id, -1e9) >= JWKS_MIN_REFETCH_S:
            self._jwks_miss[p.id] = now
            keys = await self._fetch_jwks(p, jwks_uri)
        jwk = keys.get(str(kid))
        if jwk is None:
            raise Unauthorized("id_token: signed with a key this provider does not publish")
        if jwk.get("kty") != "RSA":
            raise Unauthorized("id_token: unexpected key type")
        return RSAAlgorithm.from_jwk(json.dumps(jwk))

    # -------------------------------------------------------------- leg 1: to the provider
    async def start(self, states: OidcStateStore, provider_id: str, *, redirect_uri: str,
                    ttl_s: int) -> dict:
        p = self.provider(provider_id)
        meta = await self.metadata(p)
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, nonce = new_code_verifier(), secrets.token_urlsafe(32)
        expires_at = _now() + dt.timedelta(seconds=max(60, ttl_s))
        await states.create_state(state_hash=hash_token(state), browser_hash=hash_token(browser),
                                  provider=p.id, code_verifier=verifier, nonce=nonce,
                                  redirect_uri=redirect_uri, expires_at=expires_at)
        params = {
            "client_id": p.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": p.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
            "response_mode": "query",
            # An operator often has several accounts in the same browser; picking is better than guessing.
            "prompt": "select_account",
        }
        sep = "&" if "?" in meta["authorization_endpoint"] else "?"
        return {"authorizationUrl": f"{meta['authorization_endpoint']}{sep}{urlencode(params)}",
                "state": state, "browserToken": browser, "expiresAt": _iso(expires_at)}

    # -------------------------------------------------------------- leg 2: back from the provider
    async def complete(self, states: OidcStateStore, provider_id: str, *, state: str, code: str,
                       browser_token: str, redirect_uri: str) -> Identity:
        p = self.provider(provider_id)
        rec = await states.take_state(hash_token(state or ""), _now())
        if rec is None:
            # Expired, never issued, or already spent. One message for all three: a replay learns nothing.
            raise Unauthorized("this sign-in has expired or was already used; start again")
        if rec["provider"] != p.id:
            raise Unauthorized("this sign-in was started for a different provider")
        if not token_matches(browser_token, rec["browser_hash"]):
            raise Unauthorized("this sign-in was started in a different browser")
        if rec["redirect_uri"] != redirect_uri:
            raise Unauthorized("this sign-in was started for a different address")
        if not code:
            raise Unauthorized("the provider returned no authorization code")

        meta = await self.metadata(p)
        tokens = await self.fetcher.post_form(meta["token_endpoint"], {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": p.client_id,
            "client_secret": p.client_secret,
            "code_verifier": rec["code_verifier"],
        })
        raw = tokens.get("id_token")
        if not isinstance(raw, str) or not raw:
            raise Unauthorized("the provider returned no id_token")
        return await self.identity(p, raw, nonce=rec["nonce"])

    # -------------------------------------------------------------- id_token validation
    async def identity(self, p: Provider, raw_id_token: str, *, nonce: str) -> Identity:
        try:
            header = jwt.get_unverified_header(raw_id_token)
        except jwt.PyJWTError as e:
            raise Unauthorized("id_token: malformed") from e
        if header.get("alg") not in ALLOWED_ALGS:
            raise Unauthorized("id_token: unacceptable signature algorithm")

        # Read once *without* trusting anything, only to work out which issuer string this token claims to
        # be from. That string is then handed to the verifier, so a lie here fails the signature check.
        unverified = jwt.decode(raw_id_token, options={"verify_signature": False})
        issuer = self._expected_issuer(p, unverified, await self.metadata(p))
        key = await self._signing_key(p, header.get("kid"))
        try:
            claims = jwt.decode(
                raw_id_token, key=key, algorithms=list(ALLOWED_ALGS), audience=p.client_id,
                issuer=issuer, leeway=CLOCK_SKEW_S,
                options={"require": ["iss", "aud", "exp", "iat", "sub"], "verify_signature": True,
                         "verify_aud": True, "verify_iss": True, "verify_exp": True, "verify_iat": True},
            )
        except jwt.ExpiredSignatureError as e:
            raise Unauthorized("id_token: expired") from e
        except jwt.InvalidAudienceError as e:
            raise Unauthorized("id_token: not issued for this application") from e
        except jwt.InvalidIssuerError as e:
            raise Unauthorized("id_token: unexpected issuer") from e
        except jwt.MissingRequiredClaimError as e:
            raise Unauthorized("id_token: a required claim is missing") from e
        except jwt.InvalidSignatureError as e:
            raise Unauthorized("id_token: bad signature") from e
        except jwt.PyJWTError as e:
            raise Unauthorized("id_token: refused") from e

        # PyJWT does not know about nonce; without this check a token minted for another sign-in of the
        # same application would sail straight through.
        if not nonce or not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise Unauthorized("id_token: nonce does not match this sign-in")
        return self._identity_from_claims(p, claims)

    @staticmethod
    def _expected_issuer(p: Provider, unverified: dict, meta: dict) -> str:
        if p.id == GOOGLE:
            iss = str(unverified.get("iss") or "")
            if iss not in GOOGLE_ISSUERS:
                raise Unauthorized("id_token: unexpected issuer")
            return iss
        # Entra signs every tenant with the same key set, so "is the signature valid" says nothing about
        # *which* tenant. The tenant id decides the issuer, and only tenants this deployment named may
        # sign in at all — the mitigation for a stranger's tenant minting a token with our email in it.
        tid = str(unverified.get("tid") or "").lower()
        if tid not in p.tenants:
            raise Unauthorized("id_token: this Microsoft tenant may not sign in here")
        return str(meta["issuer"]).replace("{tenantid}", tid)

    @staticmethod
    def _identity_from_claims(p: Provider, claims: dict) -> Identity:
        email = normalize_email(str(claims.get("email") or ""))
        if not email or not EMAIL_RE.match(email):
            raise Unauthorized("the provider did not return an email address")
        name = str(claims.get("name") or "")[:120]

        if p.id == GOOGLE:
            if not _truthy(claims.get("email_verified")):
                raise Unauthorized("the provider says this email address is not verified")
            return Identity(p.id, str(claims["sub"]), email, name)

        if str(claims.get("tid") or "").lower() not in p.tenants:
            raise Unauthorized("id_token: this Microsoft tenant may not sign in here")
        # `xms_edov` is Entra's "the tenant admin proved they own this email domain". Microsoft does not
        # always emit it (it is an optional claim), so a missing value falls back to the tenant allowlist
        # above — but a value of *false* is an explicit "unverified", and that is a refusal.
        edov = claims.get("xms_edov")
        if edov is not None and not _truthy(edov):
            raise Unauthorized("the provider says this email domain is not verified")
        # `oid` is immutable and the same across applications in the tenant; `sub` is pairwise, so it
        # changes if the app registration is ever replaced. Prefer `oid` so a rotation is not a lockout.
        subject = str(claims.get("oid") or claims["sub"])
        return Identity(p.id, subject, email, name)
