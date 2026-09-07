"""Per-city daily spend, replies per session, and the counters the admin health endpoint reads.

In memory on purpose. A restart forgets the day's spend, which is why `startedAt` is part of the health
payload: an operator who sees a fresh timestamp knows the number under it is partial. The alternative —
a table written on every reply — buys accuracy across restarts that a 5 USD daily cap does not need.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class DaySpend:
    usd: float = 0.0
    calls: int = 0
    errors: int = 0
    started_at: str = field(default_factory=lambda: dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))


class BudgetStore:
    def __init__(self) -> None:
        self._days: dict[tuple[str, dt.date], DaySpend] = {}
        self._replies: dict[tuple[str, str, dt.date], int] = {}

    # ---- spend ---------------------------------------------------------------
    def today(self, city_id: str, *, day: dt.date | None = None) -> DaySpend:
        key = (city_id, day or dt.datetime.now(dt.UTC).date())
        if key not in self._days:
            self._days = {k: v for k, v in self._days.items() if k[1] >= key[1]}   # drop yesterday
            self._days[key] = DaySpend()
        return self._days[key]

    def exhausted(self, city_id: str, limit_usd: float) -> bool:
        """Checked before the call, so the last reply of the day may overshoot by one reply's cost."""
        return limit_usd > 0 and self.today(city_id).usd >= limit_usd

    def charge(self, city_id: str, usd: float, *, ok: bool = True) -> None:
        day = self.today(city_id)
        day.usd += max(usd, 0.0)
        day.calls += 1
        if not ok:
            day.errors += 1

    # ---- replies per session -------------------------------------------------
    def replies(self, city_id: str, session_id: str) -> int:
        return self._replies.get((city_id, session_id, dt.datetime.now(dt.UTC).date()), 0)

    def count_reply(self, city_id: str, session_id: str) -> int:
        today = dt.datetime.now(dt.UTC).date()
        key = (city_id, session_id, today)
        if len(self._replies) > 20000:
            self._replies = {k: v for k, v in self._replies.items() if k[2] == today}
        self._replies[key] = self._replies.get(key, 0) + 1
        return self._replies[key]


class SessionLimiter:
    """Fixed window per session. The limit is passed per call rather than fixed at construction because it
    is a per-city config value an admin can change without a restart."""

    def __init__(self, window_s: int = 60) -> None:
        self.window = window_s
        self._hits: dict[str, tuple[int, int]] = {}

    def allow(self, key: str, limit: int, now: float | None = None) -> bool:
        now = now or time.time()
        win = int(now // self.window)
        w, n = self._hits.get(key, (win, 0))
        if w != win:
            n = 0
        n += 1
        self._hits[key] = (win, n)
        if len(self._hits) > 10000:
            self._hits = {k: v for k, v in self._hits.items() if v[0] == win}
        return n <= limit
