"""Помощник тестов ``tests/_pipeline_helpers.py`` (Б-83, ревью р.1): если захват всё же выдал
чужое сообщение (параллельный прогон на той же БД вставил строку после удержания), помощник
возвращает его как было — аренда снята, попытка не израсходована — и только затем падает.

Данные выдуманные."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests import _pipeline_helpers as helpers
from tests.test_couple_functions import SM
from tests.test_pipeline_db import _pair as _db_pair
from tests.test_pipeline_db import _park, _send


async def _lease_row(engine: AsyncEngine, mid: uuid.UUID) -> tuple[Any, Any, int, str]:
    async with engine.connect() as c:
        r = (
            await c.execute(
                text(
                    "SELECT work_lease_token, work_lease_until, work_attempts, status::text "
                    "FROM partner_messages WHERE id = :m"
                ),
                {"m": mid},
            )
        ).one()
    return r[0], r[1], int(r[2]), str(r[3])


async def test_foreign_row_released_before_assertion(
    sessionmaker: SM, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Удержание «не увидело» соседнюю строку (как строку, вставленную позже другим прогоном):
    захват выдал её — помощник снимает аренду (без расхода попытки, без отсрочки) у всех
    выданных строк и падает ``AssertionError``; соседняя строка — как до захвата."""
    await _park(engine)
    anna, igor, _ = await _db_pair(sessionmaker, engine)
    target, _ = await _send(sessionmaker, engine, anna, b"a7-target")
    neighbour, _ = await _send(sessionmaker, engine, igor, b"a7-neighbour")
    before = {m: await _lease_row(engine, m) for m in (target, neighbour)}
    assert all(b[0] is None and b[3] == "processing" for b in before.values())
    # Удержание ничего не держит — как будто соседняя строка появилась после него.
    monkeypatch.setattr(helpers, "_HOLD_OTHERS", "SELECT 1 WHERE CAST(:m AS uuid) IS NULL")
    with pytest.raises(AssertionError, match="чужое сообщение"):
        await helpers.claim(engine, target)
    for m in (target, neighbour):
        token, until, attempts, status = await _lease_row(engine, m)
        assert (token, until, status) == (None, None, "processing"), m
        assert attempts == before[m][2], "попытка израсходована"
