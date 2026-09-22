"""v2.3 — APNs pushes for scheduled trips.

Two kinds, both to anonymous device tokens a phone registered itself:
* a **silent wake-up** (`content-available`) at the instants the phone asked for — twenty minutes before
  a scheduled trip leaves — so it can re-plan with live data and adjust its own reminder. The server
  never learns the trip, only "wake me at 06:52";
* an **alert push** when a new service alert touches a route the device follows.

Android needs neither: WorkManager runs the refresh and the alert poll on time without a push.
"""
import datetime as dt
import json
import logging
import time
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

    async def send(self, device_token: str, payload: dict, *, env: str = "prod", background: bool = False,
                   collapse_id: str | None = None) -> tuple[bool, str | None]:
        """(delivered, reason). `reason` is APNs' word for a failure ("BadDeviceToken", "Unregistered"…);
        the caller drops the token on those two."""
        headers = {
            "authorization": f"bearer {self.token()}",
            "apns-topic": self.bundle_id,
            "apns-push-type": "background" if background else "alert",
            "apns-priority": "5" if background else "10",
            "apns-expiration": str(int(time.time()) + (10 * 60 if background else 6 * 3600)),
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]
        url = f"{APNS_HOST.get(env, APNS_HOST['prod'])}/3/device/{device_token}"
        try:
            r = await self._cli.post(url, headers=headers, content=json.dumps(payload))
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


DEAD_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


def silent_wake_payload() -> dict:
    return {"aps": {"content-available": 1}, "kind": "tripRefresh"}


def alert_payload(alert: dict, route_names: list[str], locale: str) -> dict:
    routes = ", ".join(route_names) if route_names else ""
    title = (f"{routes}: {alert.get('header') or ''}" if routes else (alert.get("header") or "")).strip()[:120]
    body = (alert.get("description") or alert.get("header") or "")[:300]
    return {"aps": {"alert": {"title": title, "body": body}, "sound": "default", "thread-id": "route-alerts"},
            "kind": "routeAlert", "alertId": alert.get("id"), "routeIds": alert.get("routeIds") or [],
            "location": "/{city}/alerts", "locale": locale}


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
    token = str(body.get("token") or "").strip().lower()
    if not token or len(token) > 200 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("token: a hex APNs device token is required")
    platform = str(body.get("platform") or "ios").lower()
    if platform not in ("ios",):
        raise ValueError("platform: only ios registers for pushes")
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
                """SELECT token, env, city, locale, wake_at FROM push_device
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
                """SELECT token, env, city, locale, routes, alerts_sent FROM push_device
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


async def push_wakes(store: PushDeviceStore, client: ApnsClient, city: str, now: dt.datetime) -> int:
    """Silent wake-ups due now. Returns how many were delivered."""
    n = 0
    for d in await store.due_wakes(city, now):
        ok, reason = await client.send(d["token"], silent_wake_payload(), env=d.get("env", "prod"),
                                       background=True, collapse_id="tripRefresh")
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


async def push_alerts(store: PushDeviceStore, client: ApnsClient, city: str, alerts: list[dict],
                      route_names: dict[str, str], seen: set[str], today_counts: dict[str, int]) -> int:
    """New alerts to the devices following one of their routes; at most a few per device per day."""
    n = 0
    for a in alerts_to_push(alerts, seen):
        aid = str(a["id"])
        seen.add(aid)
        for d in await store.following(city, set(a["routeIds"])):
            if aid in (d.get("alerts_sent") or []) or today_counts.get(d["token"], 0) >= MAX_ALERT_PUSHES_PER_DAY:
                continue
            names = [route_names.get(r, r) for r in a["routeIds"] if r in set(d.get("routes") or [])]
            payload = alert_payload(a, names, d.get("locale") or "es")
            payload["location"] = payload["location"].replace("{city}", city)
            ok, reason = await client.send(d["token"], payload, env=d.get("env", "prod"),
                                           collapse_id=f"alert-{aid}"[:64])
            await store.record_alert(d["token"], aid)
            if ok:
                n += 1
                today_counts[d["token"]] = today_counts.get(d["token"], 0) + 1
            elif reason in DEAD_REASONS:
                await store.delete(d["token"])
    return n
