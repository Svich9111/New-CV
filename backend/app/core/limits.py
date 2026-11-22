"""Дневной лимит реплик агенту в личном чате (ТЗ §10; контракт ``RateLimited``, АР-32 п.5).

Окно — календарные сутки UTC: счётчик сбрасывается в 00:00 UTC, ``Retry-After`` — целые секунды
до этого момента (не меньше 1). Засчитываются реплики, принятые к ответу (200), включая
кризисные; реплика, получившая 429, не засчитывается. Кризисная реплика (``force=True``)
засчитывается и при исчерпанном лимите, но не отклоняется (ТЗ §9.3, §11 п.5).

Хранилище — Redis (есть в стеке, ``FEELINGS_REDIS_URL``): проверка и увеличение — один скрипт
Lua, атомарно для нескольких процессов сервера; ключ живёт до конца окна (+ запас). В тестах —
``MemoryDailyLimit`` с тем же интерфейсом (один процесс). В ключе — только id пользователя и
дата, текстов нет."""

from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

KEY_PREFIX = "feelings:chat:daily"
KEY_TTL_EXTRA_S = 3600  # ключ переживает окно на час: часы узлов могут немного расходиться


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    count: int  # реплик в окне после решения (при отказе — сколько уже было)
    retry_after_s: int  # секунды до следующего окна (≥ 1)


def window(now: datetime) -> tuple[str, int]:
    """(метка окна, секунды до его конца ≥ 1) для момента ``now`` (с часовым поясом)."""
    utc = now.astimezone(UTC)
    start = utc.replace(hour=0, minute=0, second=0, microsecond=0)
    left = (start + timedelta(days=1) - utc).total_seconds()
    return start.strftime("%Y%m%d"), max(1, math.ceil(left))


class DailyLimit(Protocol):
    async def acquire(
        self, user_id: uuid.UUID, limit: int, *, force: bool, now: datetime | None = None
    ) -> LimitDecision:
        """Засчитать реплику, если в окне меньше ``limit`` (или ``force``); иначе — отказ без
        увеличения счётчика."""
        ...


class MemoryDailyLimit:
    """Счётчик в памяти процесса — для тестов и локального запуска без Redis."""

    def __init__(self) -> None:
        self._counts: dict[tuple[uuid.UUID, str], int] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self, user_id: uuid.UUID, limit: int, *, force: bool, now: datetime | None = None
    ) -> LimitDecision:
        label, left = window(now or datetime.now(UTC))
        async with self._lock:
            key = (user_id, label)
            count = self._counts.get(key, 0)
            if not force and count >= limit:
                return LimitDecision(False, count, left)
            self._counts[key] = count + 1
            return LimitDecision(True, count + 1, left)


# KEYS[1] — ключ окна; ARGV: лимит, force (0/1), TTL. → новый счётчик или -(текущий + 1) при
# отказе (0 при отказе неотличим от «принят первым», поэтому со сдвигом).
_ACQUIRE_LUA = """
local c = tonumber(redis.call('GET', KEYS[1]) or '0')
if ARGV[2] == '0' and c >= tonumber(ARGV[1]) then
  return -(c + 1)
end
c = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return c
"""


class RedisDailyLimit:
    """Счётчик в Redis (продуктив). ``client`` — ``redis.asyncio.Redis``."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._script = client.register_script(_ACQUIRE_LUA)

    @classmethod
    def from_url(cls, url: str) -> RedisDailyLimit:
        from redis.asyncio import Redis

        return cls(Redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0))

    async def acquire(
        self, user_id: uuid.UUID, limit: int, *, force: bool, now: datetime | None = None
    ) -> LimitDecision:
        label, left = window(now or datetime.now(UTC))
        key = f"{KEY_PREFIX}:{user_id}:{label}"
        result = int(
            await self._script(keys=[key], args=[limit, 1 if force else 0, left + KEY_TTL_EXTRA_S])
        )
        if result < 0:
            return LimitDecision(False, -result - 1, left)
        return LimitDecision(True, result, left)

    async def aclose(self) -> None:
        await self._client.aclose()
