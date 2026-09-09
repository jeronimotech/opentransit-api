"""
Named admin accounts.

The first model was a single shared `ADMIN_TOKEN`: nobody could tell who changed a fare, and revoking
one operator meant rotating the secret for everybody. Operators now sign in with an email and a
password and carry a session. The shared token survives only as an explicitly-labelled *machine*
credential for CI and scripts, and a deployment can switch it off.

Two rules the rest of the code leans on:

* a password exists in clear only inside the request that carries it — what is stored is an argon2id
  digest, so a database dump cannot be turned back into logins;
* a session token is generated once and handed to the client — what is stored is its sha256 (the same
  rule as `share.py`), so a dump cannot be replayed either.

Authorisation has two independent axes, both enforced on the server: a *role* (what you may do) and a
*city scope* (where you may do it). One tenant = one city, so a scope of `[]` means "every city" and is
reserved for people who really run the whole deployment.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import logging
import re
import secrets
from typing import Any, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .db import pool
from .errors import ApiError, Forbidden

log = logging.getLogger("ot.admin_auth")

# viewer reads analytics · admin edits city config · owner also manages accounts.
ROLES = ("viewer", "admin", "owner")
MACHINE_ROLE = "machine"
# The machine credential sits at the admin level on purpose: it runs ingests and config changes for CI,
# but it is a shared secret, so it never manages accounts (see `can_manage_users`).
_RANK = {"viewer": 1, "admin": 2, MACHINE_ROLE: 2, "owner": 3}

SESSION_TOKEN_BYTES = 32
MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 200          # argon2 is happy with more; this only stops a memory-exhaustion body
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

# RFC 9106 low-memory defaults (m=64 MiB, t=3, p=4): ~50-100 ms per hash on the deployment targets,
# which is the point — a stolen digest must stay expensive to attack.
_hasher = PasswordHasher()


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    """argon2id digest, self-describing (parameters live in the string, so they can change later)."""
    validate_password(password)
    return _hasher.hash(password)


def verify_password(password: str, stored: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored)
    except InvalidHashError:
        return True


def validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise ApiError(f"password: must be at least {MIN_PASSWORD_LEN} characters", status=422)
    if len(password) > MAX_PASSWORD_LEN:
        raise ApiError(f"password: must be at most {MAX_PASSWORD_LEN} characters", status=422)


def normalize_email(email: str) -> str:
    """Case-insensitive identity: `Luis@X.com` and `luis@x.com` are the same account."""
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    e = normalize_email(email)
    if not EMAIL_RE.match(e):
        raise ApiError("email: must be an email address", status=422)
    return e


def validate_role(role: str) -> str:
    if role not in ROLES:
        raise ApiError(f"role: must be one of {', '.join(ROLES)}", status=422)
    return role


def validate_cities(cities: list[str] | None, known: set[str]) -> list[str]:
    """`[]` (or None) means every city. Anything else must name cities this deployment actually serves."""
    out = sorted({str(c).strip() for c in (cities or []) if str(c).strip()})
    unknown = [c for c in out if c not in known]
    if unknown:
        raise ApiError(f"cities: unknown city {unknown[0]}", status=422)
    return out


# ------------------------------------------------------------------ session tokens
def new_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Only the digest is stored; the token itself is never written down, logged or returned twice."""
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str | None, stored_hash: str) -> bool:
    if not token:
        return False
    return secrets.compare_digest(hash_token(token), stored_hash)


# ------------------------------------------------------------------ principals
class Principal:
    """Who is making the request. `cities` empty means every city."""

    __slots__ = ("kind", "user_id", "email", "name", "role", "cities", "session_hash")

    def __init__(self, kind: str, *, role: str, user_id: int | None = None, email: str = "",
                 name: str = "", cities: tuple[str, ...] = (), session_hash: str | None = None) -> None:
        self.kind = kind                      # "user" | "machine"
        self.role = role
        self.user_id = user_id
        self.email = email
        self.name = name
        self.cities = cities
        self.session_hash = session_hash

    @property
    def label(self) -> str:
        """What goes in an audit trail (`updatedBy`) and in logs. Never a token."""
        return self.email or (self.name or "machine")

    def has_role(self, minimum: str) -> bool:
        return _RANK.get(self.role, 0) >= _RANK.get(minimum, 99)

    def may_access(self, city_id: str) -> bool:
        return not self.cities or city_id in self.cities

    def public(self) -> dict:
        return {"id": self.user_id, "email": self.email, "name": self.name, "role": self.role,
                "cities": list(self.cities), "kind": self.kind}


def machine_principal() -> Principal:
    return Principal("machine", role=MACHINE_ROLE, name="machine token")


def principal_from_user(row: dict, session_hash: str | None = None) -> Principal:
    return Principal("user", role=row["role"], user_id=row["id"], email=row["email"], name=row["name"],
                     cities=tuple(row.get("cities") or ()), session_hash=session_hash)


def can_manage_users(p: Principal) -> bool:
    """Accounts are managed by a person, never by a shared secret."""
    return p.kind == "user" and p.role == "owner"


def visible_cities(p: Principal, all_cities: list[str]) -> list[str]:
    return sorted(c for c in all_cities if p.may_access(c))


# ------------------------------------------------------------------ failure throttle
class LoginThrottle:
    """
    Sliding count of failed logins per key (email, and separately the client address). Successful
    logins clear it, so a legitimate operator never notices; a password-guesser does. In memory only:
    a restart forgives, which is the right trade for a two-person operations team.
    """

    def __init__(self, limit: int = 10, window_s: int = 300) -> None:
        self.limit, self.window = limit, window_s
        self._fails: dict[str, list[float]] = {}

    def blocked(self, key: str, now: float) -> bool:
        hits = [t for t in self._fails.get(key, []) if now - t < self.window]
        self._fails[key] = hits
        return len(hits) >= self.limit

    def fail(self, key: str, now: float) -> None:
        self._fails.setdefault(key, []).append(now)
        if len(self._fails) > 5000:                       # cheap cleanup
            self._fails = {k: v for k, v in self._fails.items() if v and now - v[-1] < self.window}

    def clear(self, key: str) -> None:
        self._fails.pop(key, None)


# ------------------------------------------------------------------ storage
class AdminUserStore(Protocol):
    async def count_users(self) -> int: ...
    async def by_email(self, email: str) -> dict | None: ...
    async def by_id(self, user_id: int) -> dict | None: ...
    async def list_users(self) -> list[dict]: ...
    async def create_user(self, *, email: str, password_hash: str, name: str, role: str,
                          cities: list[str]) -> dict: ...
    async def update_user(self, user_id: int, **fields: Any) -> dict | None: ...
    async def touch_login(self, user_id: int) -> None: ...
    async def create_session(self, token_hash: str, user_id: int, expires_at: dt.datetime,
                             user_agent: str | None) -> None: ...
    async def session(self, token_hash: str, now: dt.datetime) -> dict | None: ...
    async def touch_session(self, token_hash: str) -> None: ...
    async def delete_session(self, token_hash: str) -> bool: ...
    async def delete_sessions_of(self, user_id: int) -> int: ...
    async def drop_expired_sessions(self) -> int: ...
    # v1.12 provider identities (Google / Microsoft). See `sign_in_with_identity`.
    async def identity(self, provider: str, subject: str) -> dict | None: ...
    async def identity_for_user(self, user_id: int, provider: str) -> dict | None: ...
    async def link_identity(self, *, provider: str, subject: str, user_id: int, email: str) -> dict: ...
    async def touch_identity(self, provider: str, subject: str, email: str) -> None: ...
    async def unlink_identity(self, user_id: int, provider: str) -> bool: ...
    async def identities_of(self, user_id: int) -> list[dict]: ...


def _iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") if t else None


def public_user(row: dict) -> dict:
    """The wire shape. `passwordHash` has no representation here by construction."""
    return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"],
            "cities": list(row.get("cities") or []), "disabled": bool(row["disabled"]),
            "createdAt": _iso(row.get("created_at")) if isinstance(row.get("created_at"), dt.datetime)
            else row.get("created_at"),
            "lastLoginAt": _iso(row.get("last_login_at")) if isinstance(row.get("last_login_at"), dt.datetime)
            else row.get("last_login_at")}


_USER_COLS = "id, email, name, role, cities, disabled, password_hash, created_at, last_login_at"
_IDENTITY_COLS = "provider, subject, user_id, email, created_at, last_login_at"


class PgAdminUserStore:
    async def count_users(self) -> int:
        async with pool().acquire() as c:
            return int(await c.fetchval("SELECT count(*) FROM admin_user") or 0)

    async def by_email(self, email: str) -> dict | None:
        async with pool().acquire() as c:
            r = await c.fetchrow(f"SELECT {_USER_COLS} FROM admin_user WHERE email_norm=$1",
                                 normalize_email(email))
        return dict(r) if r else None

    async def by_id(self, user_id: int) -> dict | None:
        async with pool().acquire() as c:
            r = await c.fetchrow(f"SELECT {_USER_COLS} FROM admin_user WHERE id=$1", user_id)
        return dict(r) if r else None

    async def list_users(self) -> list[dict]:
        async with pool().acquire() as c:
            rows = await c.fetch(f"SELECT {_USER_COLS} FROM admin_user ORDER BY email_norm")
        return [dict(r) for r in rows]

    async def create_user(self, *, email: str, password_hash: str, name: str, role: str,
                          cities: list[str]) -> dict:
        async with pool().acquire() as c:
            r = await c.fetchrow(
                """INSERT INTO admin_user (email, email_norm, password_hash, name, role, cities)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT (email_norm) DO NOTHING
                   RETURNING """ + _USER_COLS,
                email.strip(), normalize_email(email), password_hash, name, role, cities)
        if r is None:
            raise ApiError("email: an account with that email already exists", status=409, code="CONFLICT")
        return dict(r)

    async def update_user(self, user_id: int, **fields: Any) -> dict | None:
        cols = [k for k in ("name", "role", "cities", "disabled", "password_hash") if k in fields]
        if not cols:
            return await self.by_id(user_id)
        sets = ", ".join(f"{c}=${i + 2}" for i, c in enumerate(cols))
        async with pool().acquire() as c:
            r = await c.fetchrow(f"UPDATE admin_user SET {sets} WHERE id=$1 RETURNING {_USER_COLS}",
                                 user_id, *[fields[c] for c in cols])
        return dict(r) if r else None

    async def touch_login(self, user_id: int) -> None:
        async with pool().acquire() as c:
            await c.execute("UPDATE admin_user SET last_login_at=now() WHERE id=$1", user_id)

    async def create_session(self, token_hash: str, user_id: int, expires_at: dt.datetime,
                             user_agent: str | None) -> None:
        async with pool().acquire() as c:
            await c.execute(
                """INSERT INTO admin_session (token_hash, user_id, expires_at, user_agent)
                   VALUES ($1,$2,$3,$4)""", token_hash, user_id, expires_at, user_agent)

    async def session(self, token_hash: str, now: dt.datetime) -> dict | None:
        async with pool().acquire() as c:
            r = await c.fetchrow(
                """SELECT u.id, u.email, u.name, u.role, u.cities, u.disabled, u.password_hash,
                          u.created_at, u.last_login_at, s.token_hash, s.expires_at
                     FROM admin_session s JOIN admin_user u ON u.id = s.user_id
                    WHERE s.token_hash=$1 AND s.expires_at > $2""", token_hash, now)
        return dict(r) if r else None

    async def touch_session(self, token_hash: str) -> None:
        async with pool().acquire() as c:
            await c.execute("UPDATE admin_session SET last_seen_at=now() WHERE token_hash=$1", token_hash)

    async def delete_session(self, token_hash: str) -> bool:
        async with pool().acquire() as c:
            r = await c.execute("DELETE FROM admin_session WHERE token_hash=$1", token_hash)
        return r.endswith("1")

    async def delete_sessions_of(self, user_id: int) -> int:
        async with pool().acquire() as c:
            r = await c.execute("DELETE FROM admin_session WHERE user_id=$1", user_id)
        return int(r.rsplit(" ", 1)[-1] or 0)

    async def drop_expired_sessions(self) -> int:
        async with pool().acquire() as c:
            r = await c.execute("DELETE FROM admin_session WHERE expires_at <= now()")
        return int(r.rsplit(" ", 1)[-1] or 0)

    # ---------------------------------------------------------- provider identities
    async def identity(self, provider: str, subject: str) -> dict | None:
        async with pool().acquire() as c:
            r = await c.fetchrow(f"SELECT {_IDENTITY_COLS} FROM admin_identity WHERE provider=$1 AND subject=$2",
                                 provider, subject)
        return dict(r) if r else None

    async def identity_for_user(self, user_id: int, provider: str) -> dict | None:
        async with pool().acquire() as c:
            r = await c.fetchrow(f"SELECT {_IDENTITY_COLS} FROM admin_identity WHERE user_id=$1 AND provider=$2",
                                 user_id, provider)
        return dict(r) if r else None

    async def link_identity(self, *, provider: str, subject: str, user_id: int, email: str) -> dict:
        async with pool().acquire() as c:
            r = await c.fetchrow(
                """INSERT INTO admin_identity (provider, subject, user_id, email, last_login_at)
                   VALUES ($1,$2,$3,$4,now()) RETURNING """ + _IDENTITY_COLS,
                provider, subject, user_id, email)
        return dict(r)

    async def touch_identity(self, provider: str, subject: str, email: str) -> None:
        async with pool().acquire() as c:
            await c.execute("UPDATE admin_identity SET last_login_at=now(), email=$3 "
                            "WHERE provider=$1 AND subject=$2", provider, subject, email)

    async def unlink_identity(self, user_id: int, provider: str) -> bool:
        async with pool().acquire() as c:
            r = await c.execute("DELETE FROM admin_identity WHERE user_id=$1 AND provider=$2",
                                user_id, provider)
        return r.endswith("1")

    async def identities_of(self, user_id: int) -> list[dict]:
        async with pool().acquire() as c:
            rows = await c.fetch(f"SELECT {_IDENTITY_COLS} FROM admin_identity WHERE user_id=$1 ORDER BY provider",
                                 user_id)
        return [dict(r) for r in rows]


class MemoryAdminUserStore:
    """Test double with the same contract as the Postgres store."""

    def __init__(self) -> None:
        self.users: dict[int, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.identities: dict[tuple[str, str], dict] = {}
        self._next_id = 1

    async def count_users(self) -> int:
        return len(self.users)

    async def by_email(self, email: str) -> dict | None:
        norm = normalize_email(email)
        return next((copy.deepcopy(u) for u in self.users.values() if normalize_email(u["email"]) == norm), None)

    async def by_id(self, user_id: int) -> dict | None:
        u = self.users.get(user_id)
        return copy.deepcopy(u) if u else None

    async def list_users(self) -> list[dict]:
        return [copy.deepcopy(u) for u in sorted(self.users.values(), key=lambda u: normalize_email(u["email"]))]

    async def create_user(self, *, email: str, password_hash: str, name: str, role: str,
                          cities: list[str]) -> dict:
        if await self.by_email(email):
            raise ApiError("email: an account with that email already exists", status=409, code="CONFLICT")
        row = {"id": self._next_id, "email": email.strip(), "password_hash": password_hash, "name": name,
               "role": role, "cities": list(cities), "disabled": False,
               "created_at": dt.datetime.now(dt.UTC), "last_login_at": None}
        self.users[self._next_id] = row
        self._next_id += 1
        return copy.deepcopy(row)

    async def update_user(self, user_id: int, **fields: Any) -> dict | None:
        row = self.users.get(user_id)
        if row is None:
            return None
        for k in ("name", "role", "cities", "disabled", "password_hash"):
            if k in fields:
                row[k] = fields[k]
        return copy.deepcopy(row)

    async def touch_login(self, user_id: int) -> None:
        if user_id in self.users:
            self.users[user_id]["last_login_at"] = dt.datetime.now(dt.UTC)

    async def create_session(self, token_hash: str, user_id: int, expires_at: dt.datetime,
                             user_agent: str | None) -> None:
        self.sessions[token_hash] = {"token_hash": token_hash, "user_id": user_id, "expires_at": expires_at,
                                     "user_agent": user_agent, "last_seen_at": dt.datetime.now(dt.UTC)}

    async def session(self, token_hash: str, now: dt.datetime) -> dict | None:
        s = self.sessions.get(token_hash)
        if s is None or s["expires_at"] <= now:
            return None
        user = self.users.get(s["user_id"])
        if user is None:
            return None
        return {**copy.deepcopy(user), "token_hash": token_hash, "expires_at": s["expires_at"]}

    async def touch_session(self, token_hash: str) -> None:
        if token_hash in self.sessions:
            self.sessions[token_hash]["last_seen_at"] = dt.datetime.now(dt.UTC)

    async def delete_session(self, token_hash: str) -> bool:
        return self.sessions.pop(token_hash, None) is not None

    async def delete_sessions_of(self, user_id: int) -> int:
        gone = [k for k, v in self.sessions.items() if v["user_id"] == user_id]
        for k in gone:
            del self.sessions[k]
        return len(gone)

    async def drop_expired_sessions(self) -> int:
        now, before = dt.datetime.now(dt.UTC), len(self.sessions)
        self.sessions = {k: v for k, v in self.sessions.items() if v["expires_at"] > now}
        return before - len(self.sessions)

    # ---------------------------------------------------------- provider identities
    async def identity(self, provider: str, subject: str) -> dict | None:
        row = self.identities.get((provider, subject))
        return copy.deepcopy(row) if row else None

    async def identity_for_user(self, user_id: int, provider: str) -> dict | None:
        return next((copy.deepcopy(r) for r in self.identities.values()
                     if r["user_id"] == user_id and r["provider"] == provider), None)

    async def link_identity(self, *, provider: str, subject: str, user_id: int, email: str) -> dict:
        row = {"provider": provider, "subject": subject, "user_id": user_id, "email": email,
               "created_at": dt.datetime.now(dt.UTC), "last_login_at": dt.datetime.now(dt.UTC)}
        self.identities[(provider, subject)] = row
        return copy.deepcopy(row)

    async def touch_identity(self, provider: str, subject: str, email: str) -> None:
        row = self.identities.get((provider, subject))
        if row is not None:
            row["last_login_at"] = dt.datetime.now(dt.UTC)
            row["email"] = email

    async def unlink_identity(self, user_id: int, provider: str) -> bool:
        gone = [k for k, v in self.identities.items() if v["user_id"] == user_id and v["provider"] == provider]
        for k in gone:
            del self.identities[k]
        return bool(gone)

    async def identities_of(self, user_id: int) -> list[dict]:
        return [copy.deepcopy(r) for r in sorted(self.identities.values(), key=lambda r: r["provider"])
                if r["user_id"] == user_id]


# ------------------------------------------------------------------ login / bootstrap
async def open_session(store: AdminUserStore, user: dict, *, hours: int,
                       user_agent: str | None) -> tuple[str, dt.datetime]:
    """Returns the token exactly once. From here on only its digest exists anywhere."""
    token = new_session_token()
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=max(1, hours))
    await store.create_session(hash_token(token), user["id"], expires_at, (user_agent or "")[:300] or None)
    await store.touch_login(user["id"])
    return token, expires_at


async def authenticate(store: AdminUserStore, email: str, password: str) -> dict | None:
    """
    None for every failure — wrong email, wrong password, disabled account — so the response cannot be
    used to enumerate accounts. A missing account still pays for one hash, so timing cannot either.
    """
    user = await store.by_email(email)
    if user is None:
        _hasher.hash(password or "x" * MIN_PASSWORD_LEN)
        return None
    if not verify_password(password or "", user["password_hash"]):
        return None
    if user["disabled"]:
        return None
    if needs_rehash(user["password_hash"]):
        await store.update_user(user["id"], password_hash=hash_password(password))
    return user


async def bootstrap_owner(store: AdminUserStore, email: str, password: str, name: str = "") -> dict | None:
    """
    Creates the very first owner, and only that. Returns None once any account exists, which is what
    makes it safe to leave wired to environment variables on a long-lived deployment: the seed becomes
    a no-op the moment a real account is there.
    """
    if await store.count_users() > 0:
        return None
    row = await store.create_user(email=validate_email(email), password_hash=hash_password(password),
                                  name=name or "", role="owner", cities=[])
    log.info("bootstrapped the first owner account (%s)", row["email"])
    return row


# ------------------------------------------------------------------ provider sign-in (v1.12)
class ProviderIdentity(Protocol):
    """What a verified id_token yields (`app.oidc.Identity`). Declared structurally to keep the import
    one-way: oidc.py knows about accounts, accounts know nothing about OpenID Connect."""

    provider: str
    subject: str
    email: str
    name: str


async def sign_in_with_identity(store: AdminUserStore, ident: ProviderIdentity, *,
                                provision=None) -> dict:
    """
    Turn a *verified* provider identity into an account — or refuse.

    Signing in with Google or Microsoft does not create an account. It finds an existing, enabled one
    by email; an owner has to have invited that person first. `provision` is the deliberately awkward
    escape hatch for a deployment that has decided otherwise (see `domain_provisioner`), and it is
    None unless somebody configured it.

    The email is only ever the *lookup*. What the account is bound to is the provider's stable subject
    id: the first successful sign-in links it, and every later one must present the same value. Both
    directions of mismatch are refusals, never a silent re-link — that is the difference between
    "prove you still control this Google account" and "prove you control an address that spells the
    same as the one we have".
    """
    email = normalize_email(ident.email)
    user = await store.by_email(email)
    if user is None and provision is not None:
        user = await provision(ident)
    if user is None or user["disabled"]:
        # One message for both: whether an address is an admin here is not a stranger's business.
        log.info("provider sign-in refused for %s via %s (%s)", email, ident.provider,
                 "disabled" if user else "no account")
        raise Forbidden("no enabled admin account for this address — ask an owner to invite you")

    linked = await store.identity(ident.provider, ident.subject)
    if linked is not None and linked["user_id"] != user["id"]:
        log.warning("provider sign-in refused: %s identity already belongs to account %s, not %s",
                    ident.provider, linked["user_id"], user["id"])
        raise Forbidden("this provider account is already linked to a different admin account")
    if linked is None:
        existing = await store.identity_for_user(user["id"], ident.provider)
        if existing is not None and existing["subject"] != ident.subject:
            log.warning("provider sign-in refused: %s is linked to a different %s identity",
                        user["email"], ident.provider)
            raise Forbidden(f"this account is linked to a different {ident.provider} identity; "
                            "ask an owner to unlink it first")
        await store.link_identity(provider=ident.provider, subject=ident.subject, user_id=user["id"],
                                  email=email)
        log.info("linked %s identity to %s", ident.provider, user["email"])
    else:
        await store.touch_identity(ident.provider, ident.subject, email)
    return user


def domain_provisioner(store: AdminUserStore, *, domains: list[str], role: str, cities: list[str]):
    """
    Optional, off by default, and a genuinely dangerous thing to switch on: it turns "can receive mail
    at this domain" into "has an account in the admin panel". Only worth it when the domain is a
    corporate directory whose account lifecycle you actually control — and even then the role should
    be `viewer`. Returns None when no domain is configured, which is the case we expect.
    """
    allowed = {d.strip().lower().lstrip("@") for d in domains if d.strip()}
    if not allowed:
        return None
    log.warning("OIDC_AUTO_PROVISION_DOMAINS is set (%s, role=%s): anyone with a verified address at "
                "one of these domains gets an admin account on first sign-in", ", ".join(sorted(allowed)), role)

    async def provision(ident: ProviderIdentity) -> dict | None:
        email = normalize_email(ident.email)
        if email.rsplit("@", 1)[-1] not in allowed:
            return None
        # There is no password to know: the digest is of 32 random bytes nobody ever sees, so the
        # account can only ever be entered through the provider (or after an owner sets a password).
        row = await store.create_user(email=email, password_hash=_hasher.hash(secrets.token_urlsafe(32)),
                                      name=(ident.name or "")[:120], role=role, cities=list(cities))
        log.warning("auto-provisioned admin account %s (%s) from a %s sign-in", row["email"], role,
                    ident.provider)
        return row

    return provision
