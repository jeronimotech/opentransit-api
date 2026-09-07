"""
v1.7 shareable ETA links.

A trip in progress can be published under an unguessable token so anyone with the link sees where the
traveller is and when they arrive. Deliberately thin on identity:

* no session id, cohort id or analytics linkage is ever stored next to a share;
* progress coordinates are coarsened to 3 decimals (~110 m) before they touch the database, exactly like
  the analytics pipeline, so a share cannot become a precise trail;
* rows carry `expires_at` and the maintenance loop deletes them, so a forgotten link stops working;
* only the creator holds the write key, so a reader can never move somebody else's dot.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets

from .db import pool

TOKEN_BYTES = 18            # 24 url-safe chars, comfortably past the 22 the contract asks for
WRITE_KEY_BYTES = 24
MAX_ITINERARY_BYTES = 64 * 1024
PROGRESS_STATES = ("on_time", "delayed", "arrived", "cancelled")


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def new_write_key() -> str:
    return secrets.token_urlsafe(WRITE_KEY_BYTES)


def hash_key(write_key: str) -> str:
    """Only the digest is stored: a database dump cannot be used to hijack live shares."""
    return hashlib.sha256(write_key.encode()).hexdigest()


def key_matches(write_key: str | None, stored_hash: str) -> bool:
    if not write_key:
        return False
    return secrets.compare_digest(hash_key(write_key), stored_hash)


def coarse(value: float | None) -> float | None:
    """3 decimals ≈ 110 m. Same rule as analytics: a share is a reassurance, not a tracker."""
    return None if value is None else round(float(value), 3)


def clean_progress(progress: dict) -> dict:
    """Keep the documented fields only, and never store a precise position."""
    out: dict = {
        "legIndex": int(progress["legIndex"]),
        "state": progress.get("state") or "on_time",
    }
    if progress.get("atStopId"):
        out["atStopId"] = str(progress["atStopId"])[:120]
    if progress.get("etaAt"):
        out["etaAt"] = str(progress["etaAt"])[:40]
    lat, lon = coarse(progress.get("lat")), coarse(progress.get("lon"))
    if lat is not None and lon is not None:
        out["lat"], out["lon"] = lat, lon
    return out


def expiry(now: dt.datetime, ttl_minutes: int, cfg_ttl: int, cfg_max: int) -> dt.datetime:
    """Requested TTL, clamped to the city's maximum; falls back to the city default."""
    minutes = ttl_minutes or cfg_ttl
    return now + dt.timedelta(minutes=max(1, min(minutes, cfg_max)))


class MemoryShareStore:
    """Test double with the same contract as the Postgres store."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}

    async def create(self, city_id: str, token: str, key_hash: str, itinerary: dict, *,
                     label: str | None, started_at: str | None, expires_at: dt.datetime) -> None:
        self.rows[(city_id, token)] = {"token": token, "key_hash": key_hash, "itinerary": itinerary,
                                       "label": label, "started_at": started_at, "progress": None,
                                       "expires_at": expires_at, "updated_at": _utcnow()}

    async def get(self, city_id: str, token: str) -> dict | None:
        row = self.rows.get((city_id, token))
        if row is None or row["expires_at"] <= _utcnow():
            return None
        return row

    async def patch(self, city_id: str, token: str, progress: dict) -> bool:
        row = await self.get(city_id, token)
        if row is None:
            return False
        row["progress"], row["updated_at"] = progress, _utcnow()
        return True

    async def delete(self, city_id: str, token: str) -> bool:
        return self.rows.pop((city_id, token), None) is not None

    async def drop_expired(self) -> int:
        now, before = _utcnow(), len(self.rows)
        self.rows = {k: v for k, v in self.rows.items() if v["expires_at"] > now}
        return before - len(self.rows)


class PgShareStore:
    async def create(self, city_id: str, token: str, key_hash: str, itinerary: dict, *,
                     label: str | None, started_at: str | None, expires_at: dt.datetime) -> None:
        async with pool().acquire() as c:
            await c.execute(
                """INSERT INTO share_eta (city_id, token, key_hash, itinerary, label, started_at,
                                          progress, created_at, updated_at, expires_at)
                   VALUES ($1,$2,$3,$4::jsonb,$5,$6,NULL, now(), now(), $7)""",
                city_id, token, key_hash, json.dumps(itinerary), label, started_at, expires_at)

    async def get(self, city_id: str, token: str) -> dict | None:
        async with pool().acquire() as c:
            row = await c.fetchrow(
                """SELECT token, key_hash, itinerary, label, started_at, progress, updated_at, expires_at
                     FROM share_eta WHERE city_id=$1 AND token=$2 AND expires_at > now()""",
                city_id, token)
        if row is None:
            return None
        out = dict(row)
        out["itinerary"] = json.loads(out["itinerary"]) if isinstance(out["itinerary"], str) else out["itinerary"]
        if isinstance(out.get("progress"), str):
            out["progress"] = json.loads(out["progress"])
        return out

    async def patch(self, city_id: str, token: str, progress: dict) -> bool:
        async with pool().acquire() as c:
            r = await c.execute(
                """UPDATE share_eta SET progress=$3::jsonb, updated_at=now()
                    WHERE city_id=$1 AND token=$2 AND expires_at > now()""",
                city_id, token, json.dumps(progress))
        return r.endswith("1")

    async def delete(self, city_id: str, token: str) -> bool:
        async with pool().acquire() as c:
            r = await c.execute("DELETE FROM share_eta WHERE city_id=$1 AND token=$2", city_id, token)
        return r.endswith("1")

    async def drop_expired(self) -> int:
        async with pool().acquire() as c:
            r = await c.execute("DELETE FROM share_eta WHERE expires_at <= now()")
        return int(r.rsplit(" ", 1)[-1] or 0)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
