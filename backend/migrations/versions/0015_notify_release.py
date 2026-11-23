"""Уведомления 3.5, раунд 1: снятие аренды уведомления без расхода попытки (A1).

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-23 23:00:00

ФТ-18; ТЗ §4.4 п.3, §9.1 п.5, §10; DECISIONS.md АР-13, АР-38, АР-44, АР-45, АР-46; ревью 3.5 р.1
MINOR-1; BACKLOG Б-47, Б-83.

1. **``pipeline_notify_release(p_message uuid, p_token uuid, p_retry_after interval,
   p_count_attempt boolean) RETURNS boolean``** (``feelings_worker``, SECURITY DEFINER) — то же,
   что ``pipeline_release`` (0014), но для фазы уведомления: по своему токену, только у
   доставленного (``delivered``/``read``/``answered``) сообщения без ``notify_done_at``: токен
   снимается, ``work_lease_until`` := ``now() + p_retry_after`` (захват уведомления не выдаёт
   сообщение до этого времени; NULL или 0 — сразу), при ``p_count_attempt = false`` попытка не
   расходуется (счётчик −1, не ниже 0). Для 401/404 настройки бота и 429 дольше окна аренды:
   до исправления оператором уведомления ждут, а не помечаются сделанными через
   ``max_attempts`` пауз (MINOR-1). ``false`` — аренда не своя (чужой или устаревший токен,
   токен фазы конвейера), отметка уже стоит, сообщение не доставлено или удалено — ничего не
   меняется. 22023 — ``p_retry_after`` вне [0, 1 день] или ``p_count_attempt`` NULL.
   Блокировки (АР-13, АР-38): меняется одна строка сообщения (ключевые колонки не меняются —
   FK не перепроверяются), пару и пользователей не трогает — с удалением аккаунта, выходом и
   завершениями конвейера не взаимоблокируется (как ``pipeline_release`` и ``app_mark_read``).

2. **``pipeline_claim_notify``** (та же сигнатура и права): свободно, если ``work_lease_until``
   NULL или прошёл (как ``pipeline_claim`` с 0014) — срок без токена значит «не раньше». В 0013
   свободной считалась любая строка без токена, отсрочка не работала бы.

3. **Б-83 не закрывается этой миграцией**: ``pipeline_original``/``pipeline_deliver``/
   ``pipeline_withhold`` и 4-аргументная ``pipeline_withhold_leased`` ещё вызываются в коде
   (``repositories/texts.py`` — подготовка тестовых данных; тесты 11 файлов, в т.ч. A8).
   Удаление — после перевода тестов (план — BACKLOG Б-83).

Downgrade: ``pipeline_notify_release`` удаляется, ``pipeline_claim_notify`` — тело 0013.
Отложенные сроки уведомлений без токена остаются (CHECK 0014 их допускает); захват 0013 выдаёт
такие строки сразу.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
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


R13 = _rev("0013_pipeline_worker.py")

NOTIFY_RELEASE_SIGNATURE = "pipeline_notify_release(uuid, uuid, interval, boolean)"

NOTIFY_RELEASE = r"""
CREATE FUNCTION pipeline_notify_release(p_message uuid, p_token uuid, p_retry_after interval,
                                        p_count_attempt boolean) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    BEGIN
        IF p_count_attempt IS NULL OR (p_retry_after IS NOT NULL
           AND (p_retry_after < interval '0' OR p_retry_after > interval '1 day')) THEN
            RAISE EXCEPTION 'bad release' USING ERRCODE = '22023';
        END IF;
        -- Одна строка сообщения, без других блокировок (АР-13, АР-38). Фаза уведомления:
        -- доставлено и не отмечено. Срок без токена — «не раньше» (pipeline_claim_notify).
        UPDATE public.partner_messages m
           SET work_lease_token = NULL,
               work_lease_until = CASE WHEN COALESCE(p_retry_after, interval '0') > interval '0'
                                       THEN now() + p_retry_after END,
               work_attempts = CASE WHEN p_count_attempt THEN m.work_attempts
                                    ELSE GREATEST(m.work_attempts - 1, 0) END
         WHERE m.id = p_message AND m.status IN ('delivered', 'read', 'answered')
           AND m.notify_done_at IS NULL
           AND p_token IS NOT NULL AND m.work_lease_token = p_token;
        RETURN FOUND;
    END $f$
"""

_FREE_0013 = "AND (m.work_lease_token IS NULL OR m.work_lease_until < now())"
_FREE_0015 = "AND (m.work_lease_until IS NULL OR m.work_lease_until < now())"
if R13.CLAIM_NOTIFY.count(_FREE_0013) != 1:
    raise RuntimeError("тело pipeline_claim_notify 0013 не совпадает с ожидаемым")
CLAIM_NOTIFY = R13.CLAIM_NOTIFY.replace(_FREE_0013, _FREE_0015, 1).replace(
    "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
)
CLAIM_NOTIFY_0013 = R13.CLAIM_NOTIFY.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def upgrade() -> None:
    op.execute(CLAIM_NOTIFY)
    op.execute(NOTIFY_RELEASE)
    op.execute(f"REVOKE ALL ON FUNCTION {NOTIFY_RELEASE_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {NOTIFY_RELEASE_SIGNATURE} TO feelings_worker")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {NOTIFY_RELEASE_SIGNATURE}")
    op.execute(CLAIM_NOTIFY_0013)
