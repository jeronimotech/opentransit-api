"""v2.3 — pushes for scheduled trips; v2.5 — Android as well as iOS.

Two kinds, both to anonymous device tokens a phone registered itself:
* a **silent wake-up** at the instants the phone asked for — twenty minutes before a scheduled trip
  leaves — so it can re-plan with live data and adjust its own reminder. The server never learns the
  trip, only "wake me at 06:52";
* an **alert push** when a new service alert touches a route the device follows.

Both go out over whichever transport the device's platform uses: APNs for iOS, FCM (HTTP v1) for
Android. A [PushMessage] says what to deliver; each client renders it the way its service expects, so
the two passes below stay transport-agnostic and a city may have one transport, both, or neither.

Android managed without pushes until now: it schedules an exact alarm itself and WorkManager polls.
That still runs and is still the floor — the push only makes the wake-up prompt and the alert quick,
which matters on the phones whose vendors kill background work.
"""
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Protocol

import httpx
import jwt

from .db import pool

log = logging.getLogger("ot.push")

APNS_HOST = {"prod": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
WAKE_WINDOW = dt.timedelta(minutes=2)          # an instant counts as due from 2 min before it
WAKE_HORIZON = dt.timedelta(days=8)            # the phone re-registers before this runs out
MAX_WAKES = 64
MAX_ROUTES = 50
MAX_ALERT_PUSHES_PER_DAY = 6
FCM_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_-.")


# APNs says a token is gone with the first three; FCM with the rest. Either way the row goes.
DEAD_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic",
                "UNREGISTERED", "INVALID_ARGUMENT", "SENDER_ID_MISMATCH", "NOT_FOUND"}


@dataclass(frozen=True)
class PushMessage:
    """What to deliver, independent of how. `data` travels alongside in both transports; FCM needs its
    values as strings, so anything that is not one is sent as JSON and the client parses it back."""
    kind: str
    silent: bool = False
    title: str = ""
    body: str = ""
    data: dict = field(default_factory=dict)
    collapse_id: str | None = None
    ttl: int = 600                                  # seconds; an undelivered push is not worth keeping

    def apns_payload(self) -> dict:
        if self.silent:
            return {"aps": {"content-available": 1}, "kind": self.kind, **self.data}
        return {"aps": {"alert": {"title": self.title, "body": self.body}, "sound": "default",
                        "thread-id": "route-alerts"},
                "kind": self.kind, **self.data}

    def fcm_data(self) -> dict:
        """Data-only on purpose: the app renders the notification itself, so the wording follows the
        device's language and the tap opens the right screen — a `notification` block would hand both
        to the system tray and skip the app entirely while it is in the background."""
        out = {"kind": self.kind}
        if not self.silent:
            out["title"] = self.title
            out["body"] = self.body
        for k, v in self.data.items():
            out[k] = v if isinstance(v, str) else json.dumps(v)
        return out


def silent_wake_payload() -> PushMessage:
    return PushMessage(kind="tripRefresh", silent=True, collapse_id="tripRefresh", ttl=600)


def alert_payload(alert: dict, route_names: list[str], locale: str) -> PushMessage:
    routes = ", ".join(route_names) if route_names else ""
    title = (f"{routes}: {alert.get('header') or ''}" if routes else (alert.get("header") or "")).strip()[:120]
    body = (alert.get("description") or alert.get("header") or "")[:300]
    aid = str(alert.get("id") or "")
    return PushMessage(kind="routeAlert", title=title, body=body,
                       data={"alertId": alert.get("id"), "routeIds": alert.get("routeIds") or [],
                             "location": "/{city}/alerts", "locale": locale},
                       collapse_id=f"alert-{aid}"[:64], ttl=6 * 3600)


class ApnsClient:
    """Token-based APNs (ES256 JWT, refreshed every 50 minutes; Apple accepts one for an hour)."""

    def __init__(self, *, key_id: str, team_id: str, private_key: str, bundle_id: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.key_id, self.team_id, self.private_key, self.bundle_id = key_id, team_id, private_key, bundle_id
        self._jwt: str | None = None
        self._jwt_at = 0.0
        self._cli = httpx.AsyncClient(http2=transport is None, timeout=10, transport=transport)
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None

    def token(self, now: float | None = None) -> str:
        now = now or time.time()
        if self._jwt is None or now - self._jwt_at > 50 * 60:
            self._jwt = jwt.encode({"iss": self.team_id, "iat": int(now)}, self.private_key, algorithm="ES256",
                                   headers={"kid": self.key_id})
            self._jwt_at = now
        return self._jwt

    async def send(self, device_token: str, msg: PushMessage, *, env: str = "prod") -> tuple[bool, str | None]:
        """(delivered, reason). `reason` is APNs' word for a failure ("BadDeviceToken", "Unregistered"…);
        the caller drops the token on those two."""
        headers = {
            "authorization": f"bearer {self.token()}",
            "apns-topic": self.bundle_id,
            "apns-push-type": "background" if msg.silent else "alert",
            "apns-priority": "5" if msg.silent else "10",
            "apns-expiration": str(int(time.time()) + msg.ttl),
        }
        if msg.collapse_id:
            headers["apns-collapse-id"] = msg.collapse_id[:64]
        url = f"{APNS_HOST.get(env, APNS_HOST['prod'])}/3/device/{device_token}"
        try:
            r = await self._cli.post(url, headers=headers, content=json.dumps(msg.apns_payload()))
        except httpx.HTTPError as e:
            self.failed += 1
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return False, "transport"
        if r.status_code == 200:
            self.sent += 1
            return True, None
        reason = None
        try:
            reason = r.json().get("reason")
        except ValueError:
            pass
        self.failed += 1
        self.last_error = f"{r.status_code} {reason or ''}".strip()
        return False, reason or str(r.status_code)

    async def aclose(self) -> None:
        await self._cli.aclose()


class FcmClient:
    """Firebase Cloud Messaging, HTTP v1. Authentication is a service-account JWT exchanged for an
    access token, which is Google's flow and not Apple's: the JWT never goes to FCM itself."""

    TOKEN_URL = "https://oauth2.googleapis.com/token"
    SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

    def __init__(self, *, project_id: str, client_email: str, private_key: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.project_id, self.client_email, self.private_key = project_id, client_email, private_key
        self._token: str | None = None
        self._token_exp = 0.0
        self._cli = httpx.AsyncClient(timeout=10, transport=transport)
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None

    @property
    def endpoint(self) -> str:
        return f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"

    async def access_token(self, now: float | None = None) -> str:
        now = now or time.time()
        if self._token and now < self._token_exp - 120:
            return self._token
        assertion = jwt.encode(
            {"iss": self.client_email, "scope": self.SCOPE, "aud": self.TOKEN_URL,
             "iat": int(now), "exp": int(now) + 3600},
            self.private_key, algorithm="RS256")
        r = await self._cli.post(self.TOKEN_URL, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        r.raise_for_status()
        body = r.json()
        self._token = str(body["access_token"])
        self._token_exp = now + float(body.get("expires_in", 3600))
        return self._token

    @staticmethod
    def _reason(body: dict, status_code: int) -> str:
        err = body.get("error") or {}
        for d in err.get("details") or []:
            if isinstance(d, dict) and d.get("errorCode"):
                return str(d["errorCode"])
        return str(err.get("status") or status_code)

    async def send(self, device_token: str, msg: PushMessage, *, env: str = "prod") -> tuple[bool, str | None]:
        """(delivered, reason). `env` is ignored: FCM has no sandbox, the token itself says which app
        it belongs to. High priority on both kinds, because a wake-up that arrives after the bus has
        gone is worse than no wake-up at all."""
        android: dict = {"priority": "high", "ttl": f"{msg.ttl}s"}
        if msg.collapse_id:
            android["collapse_key"] = msg.collapse_id[:64]
        payload = {"message": {"token": device_token, "data": msg.fcm_data(), "android": android}}
        try:
            token = await self.access_token()
            r = await self._cli.post(self.endpoint, headers={"authorization": f"Bearer {token}"}, json=payload)
        except httpx.HTTPError as e:
            self.failed += 1
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return False, "transport"
        if r.status_code == 200:
            self.sent += 1
            return True, None
        try:
            reason = self._reason(r.json(), r.status_code)
        except ValueError:
            reason = str(r.status_code)
        self.failed += 1
        self.last_error = f"{r.status_code} {reason}".strip()
        return False, reason

    async def aclose(self) -> None:
        await self._cli.aclose()


class PushSender(Protocol):
    """What the two passes need from a transport. APNs and FCM both satisfy it."""
    sent: int
    failed: int
    last_error: str | None

    async def send(self, device_token: str, msg: PushMessage, *, env: str = "prod") -> tuple[bool, str | None]: ...
    async def aclose(self) -> None: ...


# ------------------------------------------------------------------ devices


class PushDeviceStore(Protocol):
    async def upsert(self, device: dict) -> None: ...
    async def delete(self, token: str) -> bool: ...
    async def due_wakes(self, city: str, now: dt.datetime) -> list[dict]:
        """Devices with a wake instant inside the window; the instant is consumed by `consume_wake`."""
        ...
    async def consume_wake(self, token: str, before: dt.datetime) -> None: ...
    async def following(self, city: str, route_ids: set[str]) -> list[dict]: ...
    async def record_alert(self, token: str, alert_id: str, *, keep: int = 200) -> None: ...
    async def stats(self, city: str) -> dict: ...


def _parse_instants(values) -> list[dt.datetime]:
    out = []
    for v in values or []:
        try:
            t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.UTC)
        out.append(t.astimezone(dt.UTC))
    return sorted(out)


def normalize_registration(body: dict, *, city: str, now: dt.datetime) -> dict:
    """What a phone may register: a hex token, its platform and environment, the instants it wants to be
    woken (bounded and inside the horizon) and the routes it follows (bounded). Anything else is dropped."""
    platform = str(body.get("platform") or "ios").lower()
    if platform not in ("ios", "android"):
        raise ValueError("platform: ios or android")
    token = str(body.get("token") or "").strip()
    if platform == "ios":
        # APNs device tokens are hex, and Apple accepts either case; one case in the table keeps the
        # unregister path from missing the row it means to delete.
        token = token.lower()
        if not token or len(token) > 200 or any(c not in "0123456789abcdef" for c in token):
            raise ValueError("token: a hex APNs device token is required")
    elif not token or len(token) > 512 or any(c not in FCM_TOKEN_CHARS for c in token):
        # FCM registration tokens are long, mixed-case and carry ':', '-', '_' and '.', so they are
        # neither hex nor case-insensitive: lowercasing one makes it undeliverable.
        raise ValueError("token: an FCM registration token is required")
    env = "sandbox" if str(body.get("env") or "prod").lower() == "sandbox" else "prod"
    locale = str(body.get("locale") or "es")[:5]
    wakes = [t for t in _parse_instants(body.get("wakeAt")) if now - WAKE_WINDOW <= t <= now + WAKE_HORIZON][:MAX_WAKES]
    routes = [str(r)[:80] for r in (body.get("routes") or []) if str(r).strip()][:MAX_ROUTES]
    return {"token": token, "platform": platform, "env": env, "city": city, "locale": locale,
            "wake_at": [t.isoformat() for t in wakes], "routes": routes}


class MemoryPushDeviceStore:
    def __init__(self) -> None:
        self.devices: dict[str, dict] = {}

    async def upsert(self, device):
        cur = self.devices.get(device["token"], {})
        self.devices[device["token"]] = {**cur, **device, "alerts_sent": cur.get("alerts_sent", []),
                                         "last_seen": dt.datetime.now(dt.UTC)}

    async def delete(self, token):
        return self.devices.pop(token, None) is not None

    async def due_wakes(self, city, now):
        out = []
        for d in self.devices.values():
            if d["city"] != city:
                continue
            if any(now - WAKE_WINDOW <= t <= now for t in _parse_instants(d.get("wake_at"))):
                out.append(d)
        return out

    async def consume_wake(self, token, before):
        d = self.devices.get(token)
        if d:
            d["wake_at"] = [t.isoformat() for t in _parse_instants(d.get("wake_at")) if t > before]

    async def following(self, city, route_ids):
        return [d for d in self.devices.values() if d["city"] == city and set(d.get("routes") or []) & route_ids]

    async def record_alert(self, token, alert_id, *, keep=200):
        d = self.devices.get(token)
        if d:
            d["alerts_sent"] = ([*d.get("alerts_sent", []), alert_id])[-keep:]

    async def stats(self, city):
        mine = [d for d in self.devices.values() if d["city"] == city]
        return {"devices": len(mine), "withWakes": sum(1 for d in mine if d.get("wake_at")),
                "withRoutes": sum(1 for d in mine if d.get("routes"))}


class PgPushDeviceStore:
    async def upsert(self, device):
        async with pool().acquire() as c:
            await c.execute(
                """INSERT INTO push_device (token, platform, env, city, locale, wake_at, routes)
                   VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                   ON CONFLICT (token) DO UPDATE SET platform = EXCLUDED.platform, env = EXCLUDED.env,
                     city = EXCLUDED.city, locale = EXCLUDED.locale, wake_at = EXCLUDED.wake_at,
                     routes = EXCLUDED.routes, last_seen = now()""",
                device["token"], device["platform"], device["env"], device["city"], device["locale"],
                json.dumps(device["wake_at"]), json.dumps(device["routes"]))

    async def delete(self, token):
        async with pool().acquire() as c:
            res = await c.execute("DELETE FROM push_device WHERE token=$1", token)
        return res.endswith("1")

    async def due_wakes(self, city, now):
        lo, hi = (now - WAKE_WINDOW).isoformat(), now.isoformat()
        async with pool().acquire() as c:
            rows = await c.fetch(
                """SELECT token, platform, env, city, locale, wake_at FROM push_device
                    WHERE city=$1 AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(wake_at) w
                                              WHERE w BETWEEN $2 AND $3)""", city, lo, hi)
        return [dict(r, wake_at=json.loads(r["wake_at"]) if isinstance(r["wake_at"], str) else r["wake_at"])
                for r in rows]

    async def consume_wake(self, token, before):
        async with pool().acquire() as c:
            await c.execute(
                """UPDATE push_device
                      SET wake_at = COALESCE((SELECT jsonb_agg(w) FROM jsonb_array_elements_text(wake_at) w
                                               WHERE w > $2), '[]'::jsonb)
                    WHERE token=$1""", token, before.isoformat())

    async def following(self, city, route_ids):
        async with pool().acquire() as c:
            rows = await c.fetch(
                """SELECT token, platform, env, city, locale, routes, alerts_sent FROM push_device
                    WHERE city=$1 AND routes ?| $2::text[]""", city, list(route_ids))
        out = []
        for r in rows:
            d = dict(r)
            for k in ("routes", "alerts_sent"):
                if isinstance(d[k], str):
                    d[k] = json.loads(d[k])
            out.append(d)
        return out

    async def record_alert(self, token, alert_id, *, keep=200):
        async with pool().acquire() as c:
            await c.execute(
                """UPDATE push_device SET alerts_sent = (SELECT COALESCE(jsonb_agg(x), '[]'::jsonb) FROM (
                        SELECT x FROM jsonb_array_elements(alerts_sent || to_jsonb($2::text)) x
                        ORDER BY 1 OFFSET GREATEST(jsonb_array_length(alerts_sent) + 1 - $3, 0)) s)
                    WHERE token=$1""", token, alert_id, keep)

    async def stats(self, city):
        async with pool().acquire() as c:
            row = await c.fetchrow(
                """SELECT count(*) AS devices,
                          count(*) FILTER (WHERE jsonb_array_length(wake_at) > 0) AS with_wakes,
                          count(*) FILTER (WHERE jsonb_array_length(routes) > 0) AS with_routes
                     FROM push_device WHERE city=$1""", city)
        return {"devices": row["devices"], "withWakes": row["with_wakes"], "withRoutes": row["with_routes"]}


# ------------------------------------------------------------------ the two passes


def _sender_for(senders: dict, device: dict):
    """The transport for this device, or None when its platform has none configured — which is a normal
    state, not an error: a city may run APNs only, FCM only, or neither."""
    return senders.get(str(device.get("platform") or "ios").lower())


async def push_wakes(store: PushDeviceStore, senders: dict, city: str, now: dt.datetime) -> int:
    """Silent wake-ups due now, over each device's own transport. Returns how many were delivered."""
    n = 0
    for d in await store.due_wakes(city, now):
        sender = _sender_for(senders, d)
        if sender is None:
            continue
        ok, reason = await sender.send(d["token"], silent_wake_payload(), env=d.get("env", "prod"))
        await store.consume_wake(d["token"], now)
        if ok:
            n += 1
        elif reason in DEAD_REASONS:
            await store.delete(d["token"])
    return n


def alerts_to_push(alerts: list[dict], seen: set[str]) -> list[dict]:
    """Active alerts with routes that have not been pushed yet (per city, in memory: a restart re-pushes
    nothing because each device also remembers what it received)."""
    return [a for a in alerts if a.get("id") and a.get("routeIds") and str(a["id"]) not in seen]


async def push_alerts(store: PushDeviceStore, senders: dict, city: str, alerts: list[dict],
                      route_names: dict[str, str], seen: set[str], today_counts: dict[str, int]) -> int:
    """New alerts to the devices following one of their routes; at most a few per device per day."""
    n = 0
    for a in alerts_to_push(alerts, seen):
        aid = str(a["id"])
        seen.add(aid)
        for d in await store.following(city, set(a["routeIds"])):
            sender = _sender_for(senders, d)
            if sender is None:
                continue
            if aid in (d.get("alerts_sent") or []) or today_counts.get(d["token"], 0) >= MAX_ALERT_PUSHES_PER_DAY:
                continue
            names = [route_names.get(r, r) for r in a["routeIds"] if r in set(d.get("routes") or [])]
            msg = alert_payload(a, names, d.get("locale") or "es")
            msg = replace(msg, data={**msg.data, "location": str(msg.data["location"]).replace("{city}", city)})
            ok, reason = await sender.send(d["token"], msg, env=d.get("env", "prod"))
            await store.record_alert(d["token"], aid)
            if ok:
                n += 1
                today_counts[d["token"]] = today_counts.get(d["token"], 0) + 1
            elif reason in DEAD_REASONS:
                await store.delete(d["token"])
    return n
