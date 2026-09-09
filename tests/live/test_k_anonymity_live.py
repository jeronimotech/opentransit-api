"""k-anonymity against a *real* Postgres, not the in-memory store.

The unit tests exercise `MemoryAnalyticsStore`, so the SQL that actually runs in
production was unverified — and the SQL is where the bug lived: `SUM(n) >= k`
counts events, so one person planning repeatedly published their own 150 m cell.

Needs a running API and its database:

    OT_LIVE=1 ADMIN_TOKEN=... OT_API=http://localhost:8001 \
      python -m pytest tests/live -q

It writes events into that database under a private geohash cell, so point it at
a development instance, never production.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import time
import urllib.request

import pytest

LIVE = os.environ.get("OT_LIVE") == "1"
BASE = os.environ.get("OT_API", "http://localhost:8001")
TOKEN = os.environ.get("ADMIN_TOKEN", "")
CITY = os.environ.get("OT_CITY", "bogota")

pytestmark = pytest.mark.skipif(not (LIVE and TOKEN), reason="set OT_LIVE=1 and ADMIN_TOKEN, and run the API")

# A fresh cell per run, in an area no real trip uses. A fixed cell would inherit the
# devices left by the previous run and the first assertion would fail for the wrong
# reason — as it did the first time this was written.
_J = random.randrange(0, 400)
FROM_LAT, FROM_LON = 4.5001 + _J * 0.0025, -74.2001
TO_LAT, TO_LON = 4.5101 + _J * 0.0025, -74.2101


def _post(path: str, body: dict, admin: bool = False) -> dict:
    headers = {"Content-Type": "application/json"} | ({"X-Admin-Token": TOKEN} if admin else {})
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def _get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"X-Admin-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def _batch(cohort: str, session: str, n: int, at: dt.datetime) -> dict:
    props = {"fromLat": FROM_LAT, "fromLon": FROM_LON, "toLat": TO_LAT, "toLon": TO_LON,
             "modes": ["TRANSIT", "WALK"], "fromKind": "address", "toKind": "address", "timeType": "now"}
    return {"sessionId": session, "cohortId": cohort, "platform": "ios", "appVersion": "0.0.0-test",
            "locale": "es", "sentAt": at.isoformat(),
            "events": [{"type": "plan_request", "at": at.isoformat(), "props": props} for _ in range(n)]}


def _published() -> list[dict]:
    now = dt.datetime.now(dt.UTC)
    rng = f"?from={now.date() - dt.timedelta(days=1)}&to={now.date() + dt.timedelta(days=1)}"
    od = _get(f"/v1/admin/cities/{CITY}/analytics/od{rng}")
    return [p for p in od["pairs"] if abs(p["fromCenter"]["lat"] - FROM_LAT) < 0.02]


def test_repetition_never_publishes_a_cell_and_real_devices_do():
    """One person cannot reach the threshold by repeating themselves; k devices can.

    Both halves matter: a filter that suppressed everything would also pass the
    first assertion, and that is a plausible way to get this wrong.
    """
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    marker = now.strftime("%H%M%S")

    accepted = _post(f"/v1/cities/{CITY}/events", _batch(f"coh-solo-{marker}", f"sess-solo-{marker}", 20, now))
    assert accepted["rejected"] == [], accepted
    time.sleep(2)
    assert _published() == [], "one device cleared the threshold by repeating itself"

    for i in range(5):
        _post(f"/v1/cities/{CITY}/events", _batch(f"coh-{marker}-{i}", f"sess-{marker}-{i}", 1, now))
    time.sleep(2)
    pairs = _published()
    assert pairs, "five distinct devices on the same day should publish the cell"
    assert pairs[0]["n"] >= 25, pairs[0]
