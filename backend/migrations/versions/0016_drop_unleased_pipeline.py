"""Снятие устаревших функций конвейера без проверки аренды (Б-83, A1).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-26 12:00:00

ТЗ §2 п.2, §6.2 п.3–7, §10; ФТ-09; DECISIONS.md АР-44, АР-45, АР-48; BACKLOG Б-83; ревью 3.2
MINOR-8, ревью 3.5 р.2 (тестовая доставка без снятия ``work_lease_until``).

Воркер 3.2 работает только через аренду: ``pipeline_claim`` → ``pipeline_read`` →
``pipeline_deliver_leased`` / ``pipeline_withhold_leased`` (5 аргументов, тип кризиса) /
``pipeline_release``. Прежние функции оставались для подготовки тестовых данных; код и тесты
переведены на путь с арендой (``tests/_pipeline_helpers.py``), поэтому они удаляются:

- ``pipeline_original(uuid)`` (0001, с 0014 — только без токена аренды): оригинал без проверки
  аренды и статуса черновика (Б-48 обходился бы);
- ``pipeline_deliver(uuid, bytea, boolean, smallint, boolean)`` (0005/0014): доставка без аренды,
  в распавшуюся пару доставляла (АР-44 — ``couple_ended``), не снимала ``work_lease_until``;
- ``pipeline_withhold(uuid)`` (0013/0014): остановка ``safety`` без типа кризиса (АР-45 п.3);
- ``pipeline_withhold_leased(uuid, uuid, varchar, safety_event_type[])`` — обёртка 0014 прежней
  сигнатуры 0013 (тип кризиса — первый не-``underage`` из событий); воркер с 3.5 р.1 вызывает
  5-аргументную.

Права других функций не меняются: новых прав ни у одной роли не появляется (DROP снимает только
EXECUTE удаляемых функций у ``feelings_worker``). Таблицы и данные не трогаются.

Downgrade: функции создаются заново с телами 0014 (``OLD_ORIGINAL``, ``OLD_DELIVER``,
``OLD_WITHHOLD``, ``WITHHOLD4``) и прежними правами: ``REVOKE ALL … FROM PUBLIC``, ``GRANT EXECUTE
… TO feelings_worker`` (как 0001/0013) — определение, SECURITY DEFINER, ``search_path`` и ACL
совпадают с состоянием до 0016 (тест ``test_pipeline_db.py::test_migration_0016_*``).
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rev(name: str) -> ModuleType:
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(f"_rev_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найдена ревизия: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Сигнатура → тело 0014 для downgrade (обёртка вызывает 5-аргументную, её 0016 не трогает).
def _bodies() -> dict[str, str]:
    r14 = _rev("0014_pipeline_release_crisis.py")
    return {
        "pipeline_original(uuid)": r14.OLD_ORIGINAL,
        "pipeline_deliver(uuid, bytea, boolean, smallint, boolean)": r14.OLD_DELIVER,
        "pipeline_withhold(uuid)": r14.OLD_WITHHOLD,
        "pipeline_withhold_leased(uuid, uuid, character varying, safety_event_type[])": (
            r14.WITHHOLD4
        ),
    }


REMOVED_SIGNATURES = (
    "pipeline_original(uuid)",
    "pipeline_deliver(uuid, bytea, boolean, smallint, boolean)",
    "pipeline_withhold(uuid)",
    "pipeline_withhold_leased(uuid, uuid, character varying, safety_event_type[])",
)


def upgrade() -> None:
    for sig in REMOVED_SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")


def downgrade() -> None:
    bodies = _bodies()
    if set(bodies) != set(REMOVED_SIGNATURES):
        raise RuntimeError("набор восстанавливаемых функций не совпадает со снятыми")
    for sig in REMOVED_SIGNATURES:
        op.execute(bodies[sig])
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_worker")
