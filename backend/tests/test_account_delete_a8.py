"""4.5 этап 1, A8: независимая проверка удаления аккаунта (миграция 0005, app_delete_account).

ФТ-21; ТЗ §4.5 п.3, §7 («Правила доступа»: удаление личных данных), §9.1 п.3, п.5–6, §12 п.7;
DECISIONS.md АР-13, АР-23; BACKLOG Б-3, Б-19, Б-20, Б-38.

Отличие от тестов A1 (`test_account_delete.py`): данные создаются слоем доступа приложения
(`app/repositories/users.py`, `couples.py`, `texts.py` — онбординг, пара по ссылке, шифрование
4.2, конвейер под `feelings_worker`), а не вставкой суперпользователем; поиск следов — по
текстовому представлению каждой строки каждой таблицы (все колонки любого типа: uuid, bigint,
bytea, jsonb, массивы), в том числе по фрагментам шифротекста. Гонки — свои сценарии
блокировок и свободный параллельный прогон. Действия — под ролью входа `feelings_api`
(`sessionmaker` + `user_scope`), итог — суперпользователем. Данные выдуманные (CLAUDE.md).
"""

import asyncio
import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core import crypto
from app.core.crypto import Keyring, generate_key_entry, parse_keyring
from app.db import user_scope
from app.repositories import couples, texts, users
from app.schemas import OnboardingRequest

# 2.4 (A3): отпечаток записи памяти обязателен (Б-68) — выдуманный ключ тестов.
from tests.conftest import TEST_FP
from tests.test_couple_functions import _wait_lock_waiters

SM = async_sessionmaker[AsyncSession]
DELETE = "SELECT app_delete_account()"


def _tg() -> int:
    """Telegram ID из 12 случайных цифр: при поиске по тексту строки не совпадёт случайно."""
    return 700_000_000_000 + secrets.randbelow(99_999_999_999)


def _token() -> bytes:
    return hashlib.sha256(secrets.token_bytes(32)).digest()


def _code(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


@pytest.fixture
def keys() -> Keyring:
    return parse_keyring(generate_key_entry("a8"), "a8")


async def _scalar(sm: SM, uid: uuid.UUID | None, sql: str, **params: object) -> Any:
    async with user_scope(sm, uid) as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def _outcome(sm: SM, uid: uuid.UUID | None, sql: str, **params: object) -> object:
    """Результат вызова или SQLSTATE ошибки."""
    try:
        return await _scalar(sm, uid, sql, **params)
    except DBAPIError as exc:
        return _code(exc)


async def _su(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _su_rows(engine: AsyncEngine, sql: str, **params: object) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


@asynccontextmanager
async def _worker(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Сессия конвейера (задача 3.2): роль feelings_worker."""
    async with async_sessionmaker(engine)() as s, s.begin():
        await s.execute(text("SET LOCAL ROLE feelings_worker"))
        yield s


@dataclass
class Human:
    uid: uuid.UUID
    tg: int
    agent: uuid.UUID
    conv: uuid.UUID
    marker: str
    name: str
    secrets_: dict[str, str] = field(default_factory=dict)  # известные тексты человека


async def _onboard(sm: SM, keys: Keyring, label: str, partner_name: str) -> Human:
    """Вход (app_upsert_user) → онбординг (агент) → личный чат, сводки, память — слоем доступа,
    тексты шифруются (4.2). Каждый текст содержит уникальную метку человека."""
    tg = _tg()
    uid = await users.upsert_by_telegram_id(sm, tg)
    assert uid is not None
    marker = secrets.token_hex(8)
    name = f"{label}-{marker}"
    ok = await users.complete_onboarding(
        sm,
        uid,
        OnboardingRequest(
            name=name,
            address_form="на ты",
            partner_name=partner_name,
            timezone="Europe/Moscow",
            consent_terms=True,
            consent_data=True,
            consent_sensitive=True,
            age_confirmed=True,
        ),
    )
    assert ok
    async with user_scope(sm, uid) as s:
        agent = (await s.execute(text("SELECT id FROM agents WHERE user_id = app_uid()"))).one()[0]
        conv = (
            await s.execute(
                text("INSERT INTO conversations (agent_id) VALUES (:a) RETURNING id"), {"a": agent}
            )
        ).scalar_one()
        await s.execute(
            text("INSERT INTO safety_events (user_id, type) VALUES (app_uid(), 'self_harm')")
        )
    h = Human(uid, tg, agent, conv, marker, name)
    h.secrets_ = {
        "confession": f"исповедь {label} {marker}",
        "agent_reply": f"ответ агента {label} {marker}",
        "summary": f"сводка диалога {label} {marker}",
        "profile": f"профиль {label} {marker}",
        "memory": f"память {label} {marker}",
    }
    await texts.add_conversation_message(sm, keys, uid, conv, "owner", h.secrets_["confession"])
    await texts.add_conversation_message(sm, keys, uid, conv, "agent", h.secrets_["agent_reply"])
    assert await texts.set_conversation_summary(sm, keys, uid, conv, h.secrets_["summary"])
    assert await texts.set_agent_profile_summary(sm, keys, uid, agent, h.secrets_["profile"])
    await texts.add_memory_item(
        sm, keys, uid, agent, "fact", "owner_dialog", h.secrets_["memory"], fingerprints=TEST_FP
    )
    return h


async def _deliver(engine: AsyncEngine, keys: Keyring, msg: uuid.UUID, body: str) -> None:
    async with _worker(engine) as w:
        delivered = await texts.pipeline_deliver(
            w, keys, msg, body, is_adapted=True, tone=2, harsh=False
        )
    assert delivered


async def _send(
    sm: SM, keys: Keyring, h: Human, body: str, reply_to: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    draft = await texts.create_draft(sm, keys, h.uid, h.agent, body, reply_to)
    return draft, await texts.send_draft(sm, h.uid, draft, 1)


async def _pair(sm: SM, initiator: Human, partner: Human) -> uuid.UUID:
    tok = _token()
    issued = await couples.create_with_invite(sm, initiator.uid, tok)
    await couples.accept_invite(sm, partner.uid, tok)
    return issued.couple_id


async def _from_partner_memory(
    sm: SM, keys: Keyring, owner: Human, source_msg: uuid.UUID, body: str
) -> uuid.UUID:
    """Запись памяти владельца «со слов партнёра» (ФТ-13, тип from_partner) из сообщения."""
    mid = uuid.uuid4()
    async with user_scope(sm, owner.uid) as s:
        await s.execute(
            text(
                "INSERT INTO memory_items (fingerprint, id, agent_id, type, source, source_message_id, "
                "text_enc) VALUES (sha256(convert_to(CAST(gen_random_uuid() AS text), 'UTF8')), "  # Б-68 (A1): отпечаток
                ":i, :a, 'from_partner', 'partner_message', :m, :t)"
            ),
            {
                "i": mid,
                "a": owner.agent,
                "m": source_msg,
                "t": keys.seal(crypto.MEMORY_TEXT, mid, body),
            },
        )
    return mid


# --- Поиск следов по всем строкам всех таблиц -------------------------------------------------


async def _tables(engine: AsyncEngine) -> list[str]:
    rows = await _su_rows(
        engine,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename <> 'alembic_version' ORDER BY 1",
    )
    names = [r[0] for r in rows]
    # Схема 0005: 17 таблиц ТЗ §7. Новая таблица попадёт в поиск сама.
    assert {"users", "audit_log", "payments", "kb_chunks", "prompt_versions"} <= set(names)
    return names


async def _where_found(engine: AsyncEngine, needles: dict[str, str]) -> dict[str, list[str]]:
    """{метка иглы: [таблицы]} — где игла встречается в текстовом представлении строки
    (все колонки; bytea — шестнадцатеричной записью)."""
    found: dict[str, list[str]] = {}
    tables = await _tables(engine)
    async with engine.connect() as conn:
        for label, needle in needles.items():
            for t in tables:
                n = (
                    await conn.execute(
                        text(f'SELECT count(*) FROM "{t}" r WHERE strpos(CAST(r AS text), :v) > 0'),
                        {"v": needle},
                    )
                ).scalar_one()
                if n:
                    found.setdefault(label, []).append(t)
    return found


def _hex(b: bytes) -> str:
    return b.hex()


@dataclass
class Life:
    anna: Human  # удаляет аккаунт
    igor: Human  # партнёр
    olga: Human  # посторонняя, потом — новая пара Игоря
    couple: uuid.UUID
    old_couple: uuid.UUID  # прежняя пара Анны (распущена, ссылка отозвана)
    igor_msg: uuid.UUID
    igor_reply: uuid.UUID
    igor_unsent_reply: uuid.UUID  # неотправленный черновик-ответ Игоря на сообщение Анны
    igor_from_anna_memory: uuid.UUID
    igor_texts: dict[str, str]
    subscription: uuid.UUID
    payment: uuid.UUID


async def _life(sm: SM, engine: AsyncEngine, keys: Keyring) -> Life:
    """Полный цикл: онбординг троих, прежняя ожидающая пара Анны (ссылка отозвана выходом),
    пара Игорь (инициатор) + Анна (приняла ссылку), переписка в обе стороны с конвейером,
    просмотр оригинала, ответы, память «со слов партнёра» у обоих, черновики (неотправленный,
    отклонённый, ответ), подписка и платёж с Анной-плательщиком, события безопасности."""
    anna = await _onboard(sm, keys, "Анна", "Игорь")
    igor = await _onboard(sm, keys, "Игорь", "Аня")
    olga = await _onboard(sm, keys, "Ольга", "Пётр")

    old = await couples.create_with_invite(sm, anna.uid, _token())
    await couples.leave(sm, anna.uid)  # couple.leave с actor_id Анны; ссылка отозвана
    couple = await _pair(sm, igor, anna)

    a = anna.secrets_
    a["msg"] = f"мне обидно {anna.marker}"
    a["msg_adapted"] = f"мне было обидно, давай обсудим {anna.marker}"
    a["reply"] = f"спасибо, что ответил {anna.marker}"
    a["unsent"] = f"неотправленный черновик {anna.marker}"
    a["rejected"] = f"отклонённый черновик {anna.marker}"
    a["mem_from_igor"] = f"Игорь говорил, что устал {anna.marker}"
    _, anna_msg = await _send(sm, keys, anna, a["msg"])
    await _deliver(engine, keys, anna_msg, a["msg_adapted"])
    assert await _scalar(sm, igor.uid, "SELECT app_mark_read(:m)", m=anna_msg) is True
    assert await texts.read_original(sm, keys, igor.uid, anna_msg) == a["msg"]

    it = dict(igor.secrets_)
    it["msg"] = f"я устал на работе {igor.marker}"
    it["msg_adapted"] = f"я очень устал на работе {igor.marker}"
    it["reply"] = f"прости, я не хотел {igor.marker}"
    it["reply_adapted"] = f"прости меня, я не хотел обидеть {igor.marker}"
    it["unsent_reply"] = f"ещё хочу сказать {igor.marker}"
    _, igor_reply = await _send(sm, keys, igor, it["reply"], reply_to=anna_msg)
    await _deliver(engine, keys, igor_reply, it["reply_adapted"])
    _, igor_msg = await _send(sm, keys, igor, it["msg"])
    await _deliver(engine, keys, igor_msg, it["msg_adapted"])
    igor_unsent = await texts.create_draft(
        sm, keys, igor.uid, igor.agent, it["unsent_reply"], anna_msg
    )
    # Цитата Анны в памяти Игоря — текст Анны (ФТ-13: «со слов партнёра»).
    a["igor_mem_from_anna"] = f"Анна сказала, что ей обидно {anna.marker}"
    igor_mem = await _from_partner_memory(sm, keys, igor, anna_msg, a["igor_mem_from_anna"])

    await _from_partner_memory(sm, keys, anna, igor_msg, a["mem_from_igor"])
    _, anna_reply = await _send(sm, keys, anna, a["reply"], reply_to=igor_msg)  # processing
    await texts.create_draft(sm, keys, anna.uid, anna.agent, a["unsent"])
    rejected = await texts.create_draft(sm, keys, anna.uid, anna.agent, a["rejected"])
    async with user_scope(sm, anna.uid) as s:  # «Не отправлять» (ФТ-07)
        res = await s.execute(
            text("UPDATE message_drafts SET status = 'rejected' WHERE id = :d"), {"d": rejected}
        )
        assert res.rowcount == 1  # type: ignore[attr-defined]
        # Запись журнала о себе (как запишет 2.x/4.x: кто — она, объект — она сама).
        await s.execute(
            text(
                "INSERT INTO audit_log (actor_id, action, object_type, object_id) "
                "VALUES (app_uid(), 'profile.update', 'user', app_uid())"
            )
        )
        await s.execute(
            text(
                "INSERT INTO audit_log (actor_id, action, object_type, object_id) "
                "VALUES (NULL, 'profile.view', 'user', app_uid())"
            )
        )

    sub, pay = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:  # платёжная часть — задача 4.4; пока суперпользователем
        await conn.execute(
            text(
                "INSERT INTO subscriptions (id, couple_id, payer_id, plan, status, period_start, "
                "period_end) VALUES (:s, :c, :u, 'month', 'active', now(), "
                "now() + interval '30 days')"
            ),
            {"s": sub, "c": couple, "u": anna.uid},
        )
        await conn.execute(
            text(
                "INSERT INTO payments (id, subscription_id, amount, provider, status) "
                "VALUES (:p, :s, 299, 'telegram_stars', 'succeeded')"
            ),
            {"p": pay, "s": sub},
        )
    assert anna_reply
    return Life(
        anna,
        igor,
        olga,
        couple,
        old.couple_id,
        igor_msg,
        igor_reply,
        igor_unsent,
        igor_mem,
        it,
        sub,
        pay,
    )


async def _anna_footprint(engine: AsyncEngine, life: Life) -> tuple[set[uuid.UUID], list[bytes]]:
    """Все id строк Анны и все её шифротексты/хеши (до удаления, суперпользователем)."""
    p = {"u": life.anna.uid, "a": life.anna.agent, "m": life.igor_from_anna_memory}
    ids: set[uuid.UUID] = {life.anna.uid, life.anna.agent, life.anna.conv, life.old_couple}
    blobs: list[bytes] = []
    for sql in (
        "SELECT id, text_enc FROM conversation_messages WHERE conversation_id IN "
        "(SELECT id FROM conversations WHERE agent_id = :a)",
        "SELECT id, summary_enc FROM conversations WHERE agent_id = :a",
        "SELECT id, profile_summary_enc FROM agents WHERE id = :a",
        "SELECT id, text_enc FROM memory_items WHERE agent_id = :a OR id = :m",
        "SELECT id, text_enc FROM message_drafts WHERE agent_id = :a",
        "SELECT id, original_enc FROM partner_messages WHERE author_id = :u",
        "SELECT id, adapted_enc FROM partner_messages WHERE author_id = :u",
        "SELECT id, token_hash FROM invites WHERE created_by = :u",
        "SELECT id, NULL::bytea FROM safety_events WHERE user_id = :u",
    ):
        for row_id, blob in await _su_rows(engine, sql, **p):
            ids.add(row_id)
            if blob is not None:
                blobs.append(bytes(blob))
    return ids, blobs


def _needles(life: Life, ids: set[uuid.UUID], blobs: list[bytes]) -> dict[str, str]:
    a = life.anna
    needles = {"telegram_id": str(a.tg), "name": a.name, "marker": a.marker}
    needles["marker_hex"] = _hex(a.marker.encode())
    for i in ids:
        needles[f"id:{i}"] = str(i)
    for n, blob in enumerate(blobs):
        # Середина шифротекста (тело GCM без заголовка) — уникальна для строки.
        needles[f"blob#{n}"] = _hex(blob[len(blob) // 2 - 8 : len(blob) // 2 + 8])
    for k, v in a.secrets_.items():
        needles[f"text:{k}"] = v
        needles[f"text_hex:{k}"] = _hex(v.encode())
    return needles


# --- 1. Полный цикл: следов удалённого нет нигде (критерий 1; ТЗ §7, §12 п.7) -----------------


async def test_lifecycle_delete_leaves_no_trace_anywhere(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    life = await _life(sessionmaker, engine, keys)
    ids, blobs = await _anna_footprint(engine, life)
    needles = _needles(life, ids, blobs)
    assert len(blobs) >= 14 and len(ids) >= 15  # сцена действительно наполнена
    before = await _where_found(engine, needles)
    # Контроль поиска: id, Telegram ID, имя и шифротексты находятся до удаления…
    for label in ("telegram_id", "name", *(k for k in needles if k.startswith(("id:", "blob#")))):
        assert label in before, label
    # …а открытого текста личных данных в БД нет и до удаления (шифрование, ТЗ §9.1 п.3).
    assert not [k for k in before if k.startswith(("text:", "text_hex:"))], before
    deletes_before = await _su(
        engine, "SELECT count(*) FROM audit_log WHERE action = 'account.delete'"
    )
    # Эмбеддинги (задача 2.x): у памяти Анны и у записи Игоря «со слов Анны» (ревью MINOR-5).
    vec = "[" + ",".join(["0.4242"] * 1024) + "]"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE memory_items SET embedding = CAST(:v AS vector) "
                "WHERE agent_id = :a OR id = :m"
            ),
            {"v": vec, "a": life.anna.agent, "m": life.igor_from_anna_memory},
        )
    emb_sql = "SELECT count(*) FROM memory_items WHERE embedding = CAST(:v AS vector)"
    assert await _su(engine, emb_sql, v=vec) >= 2

    await _scalar(sessionmaker, life.anna.uid, DELETE)

    assert await _su(engine, emb_sql, v=vec) == 0

    # Единственный остаток — id удалённой (ничьей) прежней пары Анны как object_id обезличенной
    # записи couple.leave (actor_id NULL; см. наблюдение О-1 в docs/reviews/4.5-test.md).
    # id пользователя, Telegram ID, тексты и шифротексты не остаются нигде.
    assert await _where_found(engine, needles) == {f"id:{life.old_couple}": ["audit_log"]}
    # Журнал: ровно одна новая запись account.delete — без id (АР-23, ТЗ §9.1 п.5).
    rows = await _su_rows(
        engine,
        "SELECT actor_id, object_type, object_id FROM audit_log WHERE action = 'account.delete' "
        "ORDER BY id DESC LIMIT 1",
    )
    assert [tuple(r) for r in rows] == [(None, "user", None)]
    deletes_after = await _su(
        engine, "SELECT count(*) FROM audit_log WHERE action = 'account.delete'"
    )
    assert deletes_after == deletes_before + 1
    # Выход из пары зафиксирован, но обезличен: обе пары Анны — couple.leave с actor_id NULL.
    leaves = await _su_rows(
        engine,
        "SELECT object_id, actor_id FROM audit_log WHERE action = 'couple.leave' "
        "AND object_id IN (:c, :o)",
        c=life.couple,
        o=life.old_couple,
    )
    assert {tuple(r) for r in leaves} == {(life.couple, None), (life.old_couple, None)}
    # Прежняя пара Анны (участников нет) удалена; пара с Игорем осталась (его история).
    assert await _su(engine, "SELECT count(*) FROM couples WHERE id = :o", o=life.old_couple) == 0
    # Вход тем же Telegram ID — новый пользователь (Б-3); старые id не возвращаются.
    new = await users.upsert_by_telegram_id(sessionmaker, life.anna.tg)
    assert new is not None and new not in ids
    assert await users.is_onboarded(sessionmaker, new) is False
    me = await users.get_me(sessionmaker, new)
    assert me is not None and me.name is None and me.has_active_couple is False


# --- 2. Партнёр: пара распущена, его данные целы и читаются (критерий 2; ТЗ §4.5 п.2–3) -----


async def test_partner_after_delete_keeps_own_data_and_can_pair_again(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    life = await _life(sessionmaker, engine, keys)
    igor, it = life.igor, life.igor_texts

    await _scalar(sessionmaker, life.anna.uid, DELETE)

    # Пара распущена для обоих; Игорь — бывший участник, «текущей» пары нет.
    assert await couples.get_current(sessionmaker, igor.uid) is None
    members = await _su_rows(
        engine,
        "SELECT c.status::text, cm.user_id, cm.active, cm.left_at IS NOT NULL FROM couples c "
        "JOIN couple_members cm ON cm.couple_id = c.id WHERE c.id = :c",
        c=life.couple,
    )
    assert [tuple(r) for r in members] == [("dissolved", igor.uid, False, True)]
    # Его сообщения Анне остались у него (recipient_id → NULL) и расшифровываются.
    rows = await _su_rows(
        engine,
        "SELECT id, recipient_id, reply_to_id, draft_id IS NOT NULL FROM partner_messages "
        "WHERE couple_id = :c ORDER BY sent_at",
        c=life.couple,
    )
    assert [tuple(r) for r in rows] == [
        (life.igor_reply, None, None, True),
        (life.igor_msg, None, None, True),
    ]
    feed = await texts.read_feed(sessionmaker, keys, igor.uid)
    assert {(f.id, f.outgoing, f.text, f.unavailable) for f in feed} == {
        (life.igor_msg, True, it["msg"], None),
        (life.igor_reply, True, it["reply"], None),
    }
    assert await texts.read_original(sessionmaker, keys, igor.uid, life.igor_msg) == it["msg"]
    # Память: своя цела; запись «со слов Анны» (цитата её сообщения) удалена — АР-23.
    mem = await texts.list_memory_items(sessionmaker, keys, igor.uid, igor.agent)
    assert [(m.type, m.text) for m in mem] == [("fact", it["memory"])]
    assert (
        await _su(
            engine, "SELECT count(*) FROM memory_items WHERE id = :m", m=life.igor_from_anna_memory
        )
        == 0
    )
    # Личный чат, сводки и черновик-ответ — на месте; ссылка ответа на удалённое → NULL.
    conv = await texts.list_conversation_messages(sessionmaker, keys, igor.uid, igor.conv)
    assert [c.text for c in conv] == [it["confession"], it["agent_reply"]]
    assert (
        await texts.get_conversation_summary(sessionmaker, keys, igor.uid, igor.conv)
        == (it["summary"])
    )
    assert (
        await texts.get_agent_profile_summary(sessionmaker, keys, igor.uid, igor.agent)
        == (it["profile"])
    )
    draft = await texts.get_draft(sessionmaker, keys, igor.uid, life.igor_unsent_reply)
    assert draft is not None and (draft.text, draft.status) == (it["unsent_reply"], "draft")
    assert (
        await _su(
            engine,
            "SELECT reply_to_id FROM message_drafts WHERE id = :d",
            d=life.igor_unsent_reply,
        )
        is None
    )
    # Подписка пары — для учёта, без плательщика; платёж на месте.
    sub = await _su_rows(
        engine,
        "SELECT s.payer_id, count(p.id) FROM subscriptions s LEFT JOIN payments p "
        "ON p.subscription_id = s.id WHERE s.id = :s GROUP BY s.payer_id",
        s=life.subscription,
    )
    assert [tuple(r) for r in sub] == [(None, 1)]
    # Отправить черновик в распущенную пару нельзя (FA003), но новая пара создаётся…
    with pytest.raises(DBAPIError) as ei:
        await texts.send_draft(sessionmaker, igor.uid, life.igor_unsent_reply, 1)
    assert _code(ei.value) == "FA003"
    new_couple = await _pair(sessionmaker, igor, life.olga)
    assert new_couple != life.couple
    current = await couples.get_current(sessionmaker, igor.uid)
    assert current is not None and current.status == "active"
    # …и тот же черновик уходит новой партнёрше: оригинал читается.
    msg = await texts.send_draft(sessionmaker, igor.uid, life.igor_unsent_reply, 1)
    assert await texts.read_original(sessionmaker, keys, igor.uid, msg) == it["unsent_reply"]


# --- 3. Повторное удаление, без входа, чужие роли (ТЗ §7, §9.2; АР-10, АР-23) ----------------


async def test_repeat_delete_and_stale_session_are_refused(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    anna = await _onboard(sessionmaker, keys, "Анна", "Игорь")
    await _scalar(sessionmaker, anna.uid, DELETE)
    # Повтор тем же id (сессия ещё жива) — 42501, ничего не пишется в журнал.
    n_audit = await _su(engine, "SELECT count(*) FROM audit_log")
    assert await _outcome(sessionmaker, anna.uid, DELETE) == "42501"
    assert await _su(engine, "SELECT count(*) FROM audit_log") == n_audit
    # Старая сессия не воскрешает пользователя и не заводит данных.
    assert await users.is_active(sessionmaker, anna.uid) is False
    assert await users.get_me(sessionmaker, anna.uid) is None
    for sql in ("SELECT app_create_couple()", "SELECT app_leave_couple()"):
        assert await _outcome(sessionmaker, anna.uid, sql) == "42501", sql
    assert (
        await _outcome(sessionmaker, anna.uid, "SELECT app_issue_invite(:h)", h=_token()) == "42501"
    )
    assert (
        await _outcome(
            sessionmaker, anna.uid, "INSERT INTO agents (user_id) VALUES (app_uid()) RETURNING 1"
        )
        == "23503"
    )
    assert await _su(engine, "SELECT count(*) FROM users WHERE id = :u", u=anna.uid) == 0


async def test_delete_without_login_unknown_user_and_marked_deleted(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    assert await _outcome(sessionmaker, None, DELETE) == "42501"
    assert await _outcome(sessionmaker, uuid.uuid4(), DELETE) == "42501"
    # Строка, помеченная deleted_at (поле §7 сохранено), функцией не трогается — 42501.
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, telegram_id, deleted_at) VALUES (:u, :t, now())"),
            {"u": uid, "t": _tg()},
        )
    assert await _outcome(sessionmaker, uid, DELETE) == "42501"
    assert await _su(engine, "SELECT count(*) FROM users WHERE id = :u", u=uid) == 1


@pytest.mark.parametrize("role", ["feelings_worker", "feelings_admin", "feelings_api", "public"])
async def test_delete_function_not_callable_by_other_roles(
    sessionmaker: SM, engine: AsyncEngine, app_engine: AsyncEngine, keys: Keyring, role: str
) -> None:
    """Только feelings_app (после SET ROLE из feelings_api) — АР-10; остальные — 42501, даже
    с выставленным app.user_id существующего пользователя."""
    victim = await _onboard(sessionmaker, keys, "Вера", "Пётр")
    eng = app_engine if role == "feelings_api" else engine
    async with eng.connect() as conn:
        if role not in ("feelings_api", "public"):
            await conn.execute(text(f"SET LOCAL ROLE {role}"))
        elif role == "public":
            await conn.execute(text("CREATE ROLE a8_nobody NOLOGIN"))
            await conn.execute(text("SET LOCAL ROLE a8_nobody"))
        await conn.execute(
            text("SELECT set_config('app.user_id', :u, true)"), {"u": str(victim.uid)}
        )
        with pytest.raises(DBAPIError) as ei:
            await conn.execute(text(DELETE))
        assert _code(ei.value) == "42501"
        await conn.rollback()
    assert await users.is_active(sessionmaker, victim.uid) is True


# --- 4. Состояния пары: ожидающая, уже распущенная, новая пара бывшего партнёра (Б-20) -------


async def test_delete_initiator_of_pending_couple_kills_invite(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    vera = await _onboard(sessionmaker, keys, "Вера", "Пётр")
    olga = await _onboard(sessionmaker, keys, "Ольга", "Пётр")
    tok = _token()
    issued = await couples.create_with_invite(sessionmaker, vera.uid, tok)
    info = await couples.find_invite(sessionmaker, olga.uid, tok)
    assert info is not None and info.status == "active"

    await _scalar(sessionmaker, vera.uid, DELETE)

    # Пара без участников и её ссылка удалены (АР-23); приглашение не находится и не
    # принимается (Б-20) — ни посторонней, ни до входа.
    for sql in (
        "SELECT count(*) FROM couples WHERE id = :c",
        "SELECT count(*) FROM invites WHERE couple_id = :c",
    ):
        assert await _su(engine, sql, c=issued.couple_id) == 0
    assert await couples.find_invite(sessionmaker, olga.uid, tok) is None
    assert await couples.find_invite(sessionmaker, None, tok) is None  # type: ignore[arg-type]
    with pytest.raises(couples.CoupleError) as ei:
        await couples.accept_invite(sessionmaker, olga.uid, tok)
    assert ei.value.sqlstate == "FB001"
    assert await couples.get_current(sessionmaker, olga.uid) is None
    # Выход вызван тем же путём, что app_leave_couple: в журнале couple.leave, обезличенный.
    leave = await _su_rows(
        engine,
        "SELECT actor_id FROM audit_log WHERE action = 'couple.leave' AND object_id = :c",
        c=issued.couple_id,
    )
    assert [tuple(r) for r in leave] == [(None,)]


async def test_delete_when_partner_left_earlier_and_then_partner_deletes(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    anna = await _onboard(sessionmaker, keys, "Анна", "Игорь")
    igor = await _onboard(sessionmaker, keys, "Игорь", "Аня")
    couple = await _pair(sessionmaker, igor, anna)
    _, msg = await _send(sessionmaker, keys, igor, f"привет {igor.marker}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO subscriptions (couple_id, payer_id, plan, status, period_start, "
                "period_end) VALUES (:c, :u, 'month', 'active', now(), now() + interval '1 day')"
            ),
            {"c": couple, "u": igor.uid},
        )
    await couples.leave(sessionmaker, igor.uid)  # Игорь вышел раньше

    await _scalar(sessionmaker, anna.uid, DELETE)  # FB007 внутри — не ошибка

    assert await _su(engine, "SELECT count(*) FROM couples WHERE id = :c", c=couple) == 1
    feed = await texts.read_feed(sessionmaker, keys, igor.uid)
    assert [(f.id, f.text) for f in feed] == [(msg, f"привет {igor.marker}")]

    await _scalar(sessionmaker, igor.uid, DELETE)

    # Второй участник ушёл — пара ничья: удалена вместе с подпиской и сообщениями.
    for sql in (
        "SELECT count(*) FROM couples WHERE id = :c",
        "SELECT count(*) FROM couple_members WHERE couple_id = :c",
        "SELECT count(*) FROM subscriptions WHERE couple_id = :c",
        "SELECT count(*) FROM partner_messages WHERE couple_id = :c",
    ):
        assert await _su(engine, sql, c=couple) == 0, sql


async def test_delete_does_not_touch_former_partners_new_couple(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Анна в распущенной паре с Игорем; Игорь уже в новой паре с Ольгой. Удаление Анны
    не распускает чужую пару и не трогает её переписку (ТЗ §4.5 п.3: «свои данные»)."""
    anna = await _onboard(sessionmaker, keys, "Анна", "Игорь")
    igor = await _onboard(sessionmaker, keys, "Игорь", "Оля")
    olga = await _onboard(sessionmaker, keys, "Ольга", "Игорь")
    await _pair(sessionmaker, anna, igor)
    await couples.leave(sessionmaker, anna.uid)
    new = await _pair(sessionmaker, igor, olga)
    _, msg = await _send(sessionmaker, keys, igor, f"Оле {igor.marker}")

    await _scalar(sessionmaker, anna.uid, DELETE)

    cur = await couples.get_current(sessionmaker, olga.uid)
    assert cur is not None and (cur.id, cur.status) == (new, "active")
    row = await _su_rows(engine, "SELECT recipient_id FROM partner_messages WHERE id = :m", m=msg)
    assert [tuple(r) for r in row] == [(olga.uid,)]


async def test_delete_without_onboarding_agent_or_couple(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """Вошёл и сразу удалил (онбординг не пройден, агента нет) — удаление работает."""
    tg = _tg()
    uid = await users.upsert_by_telegram_id(sessionmaker, tg)
    assert uid is not None
    await _scalar(sessionmaker, uid, DELETE)
    assert await _su(engine, "SELECT count(*) FROM users WHERE telegram_id = :t", t=tg) == 0


# --- 5. Б-38: черновики до отправки и «Не отправлять» (ФТ-07, ТЗ §6.2) ----------------------


async def test_unsent_and_rejected_drafts_still_deletable_sent_is_not(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    anna = await _onboard(sessionmaker, keys, "Анна", "Игорь")
    igor = await _onboard(sessionmaker, keys, "Игорь", "Аня")
    await _pair(sessionmaker, anna, igor)
    mk = {
        s: await texts.create_draft(sessionmaker, keys, anna.uid, anna.agent, f"{s} {anna.marker}")
        for s in ("draft", "approved", "rejected", "blocked")
    }
    sent, msg = await _send(sessionmaker, keys, anna, f"отправлено {anna.marker}")
    async with user_scope(sessionmaker, anna.uid) as s:
        for st in ("approved", "rejected"):  # одобрение и «Не отправлять» — владельцем
            r = await s.execute(
                text("UPDATE message_drafts SET status = CAST(:s AS draft_status) WHERE id = :d"),
                {"s": st, "d": mk[st]},
            )
            assert r.rowcount == 1, st  # type: ignore[attr-defined]
        # «Не отправлять» для уже отправленного невозможно: черновик sent не меняется.
        r = await s.execute(
            text("UPDATE message_drafts SET status = 'rejected' WHERE id = :d"), {"d": sent}
        )
        assert r.rowcount == 0  # type: ignore[attr-defined]
    async with engine.begin() as conn:  # остановлен проверкой безопасности (ТЗ §9.3)
        await conn.execute(
            text("UPDATE message_drafts SET status = 'blocked' WHERE id = :d"), {"d": mk["blocked"]}
        )
    # Партнёр чужие черновики не удаляет (0 строк), владелец — все неотправленные.
    async with user_scope(sessionmaker, igor.uid) as s:
        r = await s.execute(
            text("DELETE FROM message_drafts WHERE id = ANY(:ids)"), {"ids": list(mk.values())}
        )
        assert r.rowcount == 0  # type: ignore[attr-defined]
    async with user_scope(sessionmaker, anna.uid) as s:
        for st, d in mk.items():
            r = await s.execute(text("DELETE FROM message_drafts WHERE id = :d"), {"d": d})
            assert r.rowcount == 1, st  # type: ignore[attr-defined]
        r = await s.execute(text("DELETE FROM message_drafts WHERE id = :d"), {"d": sent})
        assert r.rowcount == 0  # type: ignore[attr-defined]
        r = await s.execute(text("DELETE FROM message_drafts"))
        assert r.rowcount == 0  # type: ignore[attr-defined]  # остался только sent
    assert (
        await texts.read_original(sessionmaker, keys, anna.uid, msg) == f"отправлено {anna.marker}"
    )


# --- 6. Гонки: отправка партнёром одновременно с удалением (критерий 5; АР-13) -------------


async def _ready_pair(sm: SM, keys: Keyring) -> tuple[Human, Human, uuid.UUID, uuid.UUID]:
    anna = await _onboard(sm, keys, "Анна", "Игорь")
    igor = await _onboard(sm, keys, "Игорь", "Аня")
    couple = await _pair(sm, anna, igor)
    draft = await texts.create_draft(sm, keys, igor.uid, igor.agent, f"в гонке {igor.marker}")
    return anna, igor, couple, draft


async def test_race_send_waits_for_delete_gets_fa003(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Удаление первым: оно держит строку Анны (FOR UPDATE) и ждёт пару (её держит третья
    транзакция); отправка Игоря встаёт на строке Анны (FOR KEY SHARE). Итог — FA003, черновик
    не отправлен и цел, без 40P01/23503."""
    anna, igor, couple, draft = await _ready_pair(sessionmaker, keys)
    async with engine.connect() as holder:
        await holder.execute(text("SELECT 1 FROM couples WHERE id = :c FOR UPDATE"), {"c": couple})
        t_del = asyncio.ensure_future(_outcome(sessionmaker, anna.uid, DELETE))
        await _wait_lock_waiters(engine, "app_delete_account", 1)
        t_send = asyncio.ensure_future(
            _outcome(sessionmaker, igor.uid, "SELECT app_send_draft(:d, 1)", d=draft)
        )
        await _wait_lock_waiters(engine, "app_send_draft", 1)
        await holder.rollback()
    res_del, res_send = await asyncio.wait_for(asyncio.gather(t_del, t_send), timeout=30)
    assert (res_del, res_send) == (None, "FA003")
    d = await texts.get_draft(sessionmaker, keys, igor.uid, draft)
    assert d is not None and (d.status, d.text) == ("draft", f"в гонке {igor.marker}")
    assert (
        await _su(engine, "SELECT count(*) FROM partner_messages WHERE couple_id = :c", c=couple)
        == 0
    )


async def test_race_delete_waits_for_send_message_stays_with_author(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Отправка первой: она взяла FOR KEY SHARE на строки обоих и ждёт черновик (его держит
    третья транзакция); удаление Анны ждёт её строку. Сообщение доставлено в пару, затем пара
    распущена: оно остаётся у Игоря без получателя и читается."""
    anna, igor, couple, draft = await _ready_pair(sessionmaker, keys)
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM message_drafts WHERE id = :d FOR UPDATE"), {"d": draft}
        )
        t_send = asyncio.ensure_future(
            _outcome(sessionmaker, igor.uid, "SELECT app_send_draft(:d, 1)", d=draft)
        )
        await _wait_lock_waiters(engine, "app_send_draft", 1)
        t_del = asyncio.ensure_future(_outcome(sessionmaker, anna.uid, DELETE))
        await _wait_lock_waiters(engine, "app_delete_account", 1)
        await holder.rollback()
    res_send, res_del = await asyncio.wait_for(asyncio.gather(t_send, t_del), timeout=30)
    assert res_del is None
    assert isinstance(res_send, uuid.UUID)
    row = await _su_rows(
        engine,
        "SELECT couple_id, author_id, recipient_id FROM partner_messages WHERE id = :m",
        m=res_send,
    )
    assert [tuple(r) for r in row] == [(couple, igor.uid, None)]
    assert await texts.read_original(sessionmaker, keys, igor.uid, res_send) == (
        f"в гонке {igor.marker}"
    )


async def test_race_free_running_send_deliver_delete(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Без принудительного порядка, 12 прогонов: Игорь отправляет черновик, конвейер
    доставляет его ответ на сообщение Анны (ответ → исходное, pipeline_deliver), Анна удаляет
    аккаунт. Допустимы только: удаление успешно; отправка — id сообщения или FA003;
    доставка — True. Ни 40P01, ни 23503, ни сообщений Анны после."""
    seen_send: set[str] = set()
    for _ in range(12):
        anna, igor, couple, draft = await _ready_pair(sessionmaker, keys)
        _, anna_msg = await _send(sessionmaker, keys, anna, f"исходное {anna.marker}")
        await _deliver(engine, keys, anna_msg, f"адаптировано {anna.marker}")
        _, reply = await _send(sessionmaker, keys, igor, f"ответ {igor.marker}", anna_msg)

        async def deliver(
            reply: uuid.UUID = reply, body: str = f"адаптированный ответ {igor.marker}"
        ) -> object:
            try:
                async with _worker(engine) as w:
                    return await texts.pipeline_deliver(
                        w,
                        keys,
                        reply,
                        body,
                        is_adapted=True,
                        tone=1,
                        harsh=False,
                    )
            except DBAPIError as exc:
                return _code(exc)

        res = await asyncio.wait_for(
            asyncio.gather(
                _outcome(sessionmaker, igor.uid, "SELECT app_send_draft(:d, 1)", d=draft),
                deliver(),
                _outcome(sessionmaker, anna.uid, DELETE),
            ),
            timeout=30,
        )
        sent, delivered, deleted = res
        assert deleted is None, res
        assert delivered is True, res
        assert isinstance(sent, uuid.UUID) or sent == "FA003", res
        seen_send.add("ok" if isinstance(sent, uuid.UUID) else str(sent))
        left = await _su_rows(
            engine,
            "SELECT author_id, recipient_id, reply_to_id, status::text FROM partner_messages "
            "WHERE couple_id = :c",
            c=couple,
        )
        assert all(r.author_id == igor.uid and r.recipient_id is None for r in left), left
        assert all(r.reply_to_id is None for r in left), left
        assert await couples.get_current(sessionmaker, igor.uid) is None
    assert seen_send <= {"ok", "FA003"}


async def test_send_draft_refuses_without_login_or_marked_deleted_sender(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Новые коды app_send_draft (0005, АР-13): без входа и для помеченного deleted_at
    отправителя — 42501 (раньше FA001); черновик не отправлен (ревью A6 MINOR-4)."""
    anna, igor, _, draft = await _ready_pair(sessionmaker, keys)
    assert await _outcome(sessionmaker, None, "SELECT app_send_draft(:d, 1)", d=draft) == "42501"
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": igor.uid}
        )
    assert (
        await _outcome(sessionmaker, igor.uid, "SELECT app_send_draft(:d, 1)", d=draft) == "42501"
    )
    assert (
        await _su(engine, "SELECT status::text FROM message_drafts WHERE id = :d", d=draft)
        == "draft"
    )
    assert anna


async def _hold_rows_and_race(
    engine: AsyncEngine, msgs: list[uuid.UUID], steps: list[tuple[str, Any]]
) -> list[object]:
    """Третья транзакция держит строки сообщений; шаги стартуют по очереди, каждый — когда
    предыдущий встал в ожидание; затем блокировки снимаются (порядок задан, не случаен)."""
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM partner_messages WHERE id = ANY(:m) FOR UPDATE"), {"m": msgs}
        )
        tasks: list[asyncio.Future[object]] = []
        try:
            for n, (func, coro) in enumerate(steps, start=1):
                tasks.append(asyncio.ensure_future(coro))
                same = sum(1 for f, _ in steps[:n] if f == func)
                await _wait_lock_waiters(engine, func, same)
        finally:
            await holder.rollback()
        return list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=30))


async def _cross_replies(
    sm: SM, engine: AsyncEngine, keys: Keyring
) -> tuple[Human, Human, uuid.UUID, uuid.UUID, uuid.UUID]:
    """X (Анна) ← Y (ответ Игоря, доставлен) ← Z (ответ Анны, ещё в конвейере)."""
    anna = await _onboard(sm, keys, "Анна", "Игорь")
    igor = await _onboard(sm, keys, "Игорь", "Аня")
    await _pair(sm, anna, igor)
    _, x = await _send(sm, keys, anna, f"X {anna.marker}")
    await _deliver(engine, keys, x, f"X* {anna.marker}")
    _, y = await _send(sm, keys, igor, f"Y {igor.marker}", reply_to=x)
    await _deliver(engine, keys, y, f"Y* {igor.marker}")
    _, z = await _send(sm, keys, anna, f"Z {anna.marker}", reply_to=y)
    return anna, igor, x, y, z


async def _deliver_outcome(engine: AsyncEngine, keys: Keyring, msg: uuid.UUID) -> object:
    try:
        async with _worker(engine) as w:
            return await texts.pipeline_deliver(
                w, keys, msg, "версия для получателя", is_adapted=False, tone=0, harsh=False
            )
    except DBAPIError as exc:
        return _code(exc)


async def test_race_delete_vs_pipeline_delivering_own_reply_no_deadlock(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Анна удаляет аккаунт, пока конвейер доставляет её ответ Z на ответ Игоря Y (Y отвечает
    на её X). Карточка: «без взаимоблокировок»; АР-23 — «без взаимоблокировки с конвейером».
    Допустимо: удаление успешно, доставка True/False (сообщения уже нет) — но не 40P01."""
    anna, _igor, _x, y, z = await _cross_replies(sessionmaker, engine, keys)
    res = await _hold_rows_and_race(
        engine,
        [y],
        [
            ("app_delete_account", _outcome(sessionmaker, anna.uid, DELETE)),
            ("pipeline_deliver", _deliver_outcome(engine, keys, z)),
        ],
    )
    assert res[0] is None and res[1] in (True, False), res


async def test_race_both_former_partners_delete_with_cross_replies_no_deadlock(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    anna, igor, _x, y, z = await _cross_replies(sessionmaker, engine, keys)
    await _deliver(engine, keys, z, "Z*")
    await couples.leave(sessionmaker, igor.uid)  # пара распущена раньше
    res = await _hold_rows_and_race(
        engine,
        [y, z],
        [
            ("app_delete_account", _outcome(sessionmaker, anna.uid, DELETE)),
            ("app_delete_account", _outcome(sessionmaker, igor.uid, DELETE)),
        ],
    )
    assert res == [None, None], res
    n = await _su(engine, "SELECT count(*) FROM users WHERE id IN (:a, :b)", a=anna.uid, b=igor.uid)
    assert n == 0


# --- 7. Дефект: журнал принимает id удалённого пользователя от «живой» старой сессии ---------


async def test_stale_session_cannot_put_deleted_id_back_into_audit(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """АР-23: после удаления id пользователя в audit_log не остаётся. Запрос, начатый до
    удаления и записывающий аудит после, не должен вернуть id в журнал (защита в глубину:
    сейчас записи аудита пишут только функции БД, а deps.py отклоняет удалённых)."""
    anna = await _onboard(sessionmaker, keys, "Анна", "Игорь")
    await _scalar(sessionmaker, anna.uid, DELETE)
    await _outcome(
        sessionmaker,
        anna.uid,
        "INSERT INTO audit_log (actor_id, action, object_type) "
        "VALUES (app_uid(), 'me.export', 'user') RETURNING 1",
    )
    n = await _su(engine, "SELECT count(*) FROM audit_log WHERE actor_id = :u", u=anna.uid)
    assert n == 0


# --- 8. Раунд 2 (после 54c83ca): конвейер при удалённом получателе, журнал, гонки -----------
#
# Исправления A1: app_delete_account блокирует все свои пары FOR UPDATE до строк сообщений;
# pipeline_deliver/withhold первым шагом берут пару FOR KEY SHARE; сообщения партнёра меняются
# одной командой; адаптация у сообщений удалённому обнуляется; audit_insert требует
# существующего пользователя (EXISTS … FOR KEY SHARE). ТЗ §6.2 п.4–6, §7, §9.1 п.5; АР-13, АР-23.

WITHHOLD = "SELECT pipeline_withhold(:m)"
LEAVE = "SELECT app_leave_couple()"
# RETURNING 1: у feelings_app нет SELECT на audit_log (журнал только на запись).
AUDIT_SELF = (
    "INSERT INTO audit_log (actor_id, action, object_type) "
    "VALUES (app_uid(), 'me.export', 'user') RETURNING 1"
)
_BAD = {"40P01", "23503", "40001"}  # взаимоблокировка, FK, сериализация — не должно быть


async def _withhold_outcome(engine: AsyncEngine, msg: uuid.UUID) -> object:
    try:
        async with _worker(engine) as w:
            return (await w.execute(text(WITHHOLD), {"m": msg})).scalar_one()
    except DBAPIError as exc:
        return _code(exc)


async def _deliver_adapted(
    engine: AsyncEngine, keys: Keyring, msg: uuid.UUID, body: str, *, tone: int = 3
) -> object:
    """Доставка адаптированной версии (is_adapted, тон, предупреждение) — результат или код."""
    try:
        async with _worker(engine) as w:
            return await texts.pipeline_deliver(
                w, keys, msg, body, is_adapted=True, tone=tone, harsh=True
            )
    except DBAPIError as exc:
        return _code(exc)


async def _hold_race(
    engine: AsyncEngine, lock_sql: str, params: dict[str, object], steps: list[tuple[str, Any]]
) -> list[object]:
    """Третья транзакция держит строки (lock_sql); шаги стартуют по очереди, каждый — когда
    предыдущий встал в ожидание блокировки; затем держатель отпускает."""
    async with engine.connect() as holder:
        await holder.execute(text(lock_sql), params)
        tasks: list[asyncio.Future[object]] = []
        try:
            for n, (func, coro) in enumerate(steps, start=1):
                tasks.append(asyncio.ensure_future(coro))
                same = sum(1 for f, _ in steps[:n] if f == func)
                await _wait_lock_waiters(engine, func, same)
        finally:
            await holder.rollback()
        return list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=30))


async def _msg_row(engine: AsyncEngine, msg: uuid.UUID) -> dict[str, Any] | None:
    rows = await _su_rows(
        engine,
        "SELECT author_id, recipient_id, reply_to_id, status::text AS status, "
        "adapted_enc IS NOT NULL AS has_adapted, is_adapted, tone_score, harsh_tone_warning "
        "FROM partner_messages WHERE id = :m",
        m=msg,
    )
    return dict(rows[0]._mapping) if rows else None


def _no_recipient_version(author: uuid.UUID, status: str) -> dict[str, Any]:
    return {
        "author_id": author,
        "recipient_id": None,
        "reply_to_id": None,
        "status": status,
        "has_adapted": False,
        "is_adapted": False,
        "tone_score": None,
        "harsh_tone_warning": False,
    }


async def test_r2_withhold_after_recipient_deleted_stays_with_author(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """ТЗ §9.3, §6.2 п.3: остановка безопасностью после удаления получателя работает;
    сообщение остаётся у автора («остановлено»), без версии для получателя, оригинал читается.
    Сообщение удалённого автора (ещё в конвейере) — withhold и deliver дают false: его нет."""
    anna, igor, _couple, _ = await _ready_pair(sessionmaker, keys)
    _, to_anna = await _send(sessionmaker, keys, igor, f"Анне {igor.marker}")
    _, from_anna = await _send(sessionmaker, keys, anna, f"Игорю {anna.marker}")

    await _scalar(sessionmaker, anna.uid, DELETE)

    assert await _withhold_outcome(engine, to_anna) is True
    assert await _msg_row(engine, to_anna) == _no_recipient_version(igor.uid, "withheld")
    feed = await texts.read_feed(sessionmaker, keys, igor.uid)
    assert [(f.id, f.outgoing, f.text, f.is_adapted, f.status) for f in feed] == [
        (to_anna, True, f"Анне {igor.marker}", False, "withheld")
    ]
    assert await texts.read_original(sessionmaker, keys, igor.uid, to_anna) == (
        f"Анне {igor.marker}"
    )
    # Повторная остановка/доставка того же — false (уже не в конвейере).
    assert await _withhold_outcome(engine, to_anna) is False
    assert await _deliver_adapted(engine, keys, to_anna, "поздно") is False
    # Сообщения удалённого автора нет — конвейер получает false, а не ошибку.
    assert await _withhold_outcome(engine, from_anna) is False
    assert await _deliver_adapted(engine, keys, from_anna, "поздно") is False
    assert await _msg_row(engine, from_anna) is None


@pytest.mark.parametrize("op", ["withhold", "deliver"])
@pytest.mark.parametrize("first", ["delete", "pipeline"])
async def test_r2_race_pipeline_vs_recipient_delete_ordered(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring, op: str, first: str
) -> None:
    """Сообщение Игоря Анне в конвейере; Анна удаляет аккаунт. Порядок задан держателем
    строки сообщения: первый встаёт на ней, второй — на паре. В обоих порядках без 40P01;
    итог одинаков: сообщение у Игоря, получателя и версии для получателя нет (АР-23,
    ревью A6 MINOR-3) — даже если доставка успела записать адаптацию раньше удаления."""
    anna, igor, _couple, _ = await _ready_pair(sessionmaker, keys)
    _, m = await _send(sessionmaker, keys, igor, f"в конвейере {igor.marker}")
    fn = "pipeline_withhold" if op == "withhold" else "pipeline_deliver"
    pipe = (
        _withhold_outcome(engine, m)
        if op == "withhold"
        else _deliver_adapted(engine, keys, m, f"мягче {igor.marker}")
    )
    delete = ("app_delete_account", _outcome(sessionmaker, anna.uid, DELETE))
    steps = [delete, (fn, pipe)] if first == "delete" else [(fn, pipe), delete]
    res = await _hold_race(
        engine, "SELECT 1 FROM partner_messages WHERE id = :m FOR UPDATE", {"m": m}, steps
    )
    got = dict(zip([s[0] for s in steps], res, strict=True))
    assert got == {"app_delete_account": None, fn: True}, got
    status = "withheld" if op == "withhold" else "delivered"
    assert await _msg_row(engine, m) == _no_recipient_version(igor.uid, status)
    feed = await texts.read_feed(sessionmaker, keys, igor.uid)
    assert [(f.id, f.text, f.is_adapted) for f in feed] == [
        (m, f"в конвейере {igor.marker}", False)
    ]


async def test_r2_active_pair_pipeline_still_adapts_and_marks(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Регрессия конвейера 3.2 при живом получателе (ТЗ §6.2 п.4–6, принцип 3): версия для
    получателя, пометка «адаптировано», тон и предупреждение сохраняются; оригинал — по запросу;
    автор видит свой оригинал. Копия без адаптации — is_adapted false. Выход из пары (не
    удаление) версию не трогает; удаление постороннего — тоже."""
    anna, igor, couple, _ = await _ready_pair(sessionmaker, keys)
    olga = await _onboard(sessionmaker, keys, "Ольга", "Пётр")
    _, m1 = await _send(sessionmaker, keys, anna, f"резко {anna.marker}")
    # Посторонняя удаляет аккаунт одновременно с доставкой в чужой паре.
    d1, del_olga = await asyncio.gather(
        _deliver_adapted(engine, keys, m1, f"мягко {anna.marker}", tone=3),
        _outcome(sessionmaker, olga.uid, DELETE),
    )
    assert (d1, del_olga) == (True, None)
    _, m2 = await _send(sessionmaker, keys, anna, f"как есть {anna.marker}")
    async with _worker(engine) as w:
        assert await texts.pipeline_deliver(
            w, keys, m2, f"как есть {anna.marker}", is_adapted=False, tone=0, harsh=False
        )
    r1 = await _msg_row(engine, m1)
    assert r1 is not None
    assert (r1["recipient_id"], r1["status"], r1["has_adapted"], r1["is_adapted"]) == (
        igor.uid,
        "delivered",
        True,
        True,
    )
    assert (r1["tone_score"], r1["harsh_tone_warning"]) == (3, True)
    r2 = await _msg_row(engine, m2)
    assert r2 is not None and (r2["has_adapted"], r2["is_adapted"], r2["tone_score"]) == (
        True,
        False,
        0,
    )
    feed_igor = {
        f.id: (f.outgoing, f.text, f.is_adapted)
        for f in await texts.read_feed(sessionmaker, keys, igor.uid)
    }
    assert feed_igor == {
        m1: (False, f"мягко {anna.marker}", True),
        m2: (False, f"как есть {anna.marker}", False),
    }
    assert await texts.read_original(sessionmaker, keys, igor.uid, m1) == f"резко {anna.marker}"
    feed_anna = {
        f.id: (f.outgoing, f.text) for f in await texts.read_feed(sessionmaker, keys, anna.uid)
    }
    assert feed_anna == {m1: (True, f"резко {anna.marker}"), m2: (True, f"как есть {anna.marker}")}
    # Выход из пары — не удаление: получатель остаётся, версия для получателя и пометка тоже.
    _, m3 = await _send(sessionmaker, keys, igor, f"последнее {igor.marker}")
    await couples.leave(sessionmaker, anna.uid)
    assert await _deliver_adapted(engine, keys, m3, f"мягко {igor.marker}") is True
    r3 = await _msg_row(engine, m3)
    assert r3 is not None and (r3["recipient_id"], r3["has_adapted"], r3["is_adapted"]) == (
        anna.uid,
        True,
        True,
    )
    assert (await _msg_row(engine, m1) or {})["is_adapted"] is True
    assert couple


@pytest.mark.parametrize("first", ["leave", "deliver"])
async def test_r2_race_leave_vs_pipeline_delivering_reply_no_deadlock(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring, first: str
) -> None:
    """Игорь выходит из пары, пока конвейер доставляет ответ Анны Z на его Y (X ← Y ← Z).
    Выход берёт пару FOR UPDATE, конвейер — пару FOR KEY SHARE, затем Z и Y. Без 40P01 в обоих
    порядках; доставка проходит с адаптацией (получатель жив), Y — «отвечено»."""
    anna, igor, _x, y, z = await _cross_replies(sessionmaker, engine, keys)
    couple = await _su(engine, "SELECT couple_id FROM partner_messages WHERE id = :m", m=z)
    leave = ("app_leave_couple", _outcome(sessionmaker, igor.uid, LEAVE))
    deliver = ("pipeline_deliver", _deliver_adapted(engine, keys, z, f"Z* {anna.marker}"))
    if first == "deliver":
        res = await _hold_race(
            engine,
            "SELECT 1 FROM partner_messages WHERE id = :y FOR UPDATE",
            {"y": y},
            [deliver, leave],
        )
        got = {"pipeline_deliver": res[0], "app_leave_couple": res[1]}
    else:
        res = await _hold_race(
            engine,
            "SELECT 1 FROM couples WHERE id = :c FOR UPDATE",
            {"c": couple},
            [leave, deliver],
        )
        got = {"app_leave_couple": res[0], "pipeline_deliver": res[1]}
    assert got == {"app_leave_couple": couple, "pipeline_deliver": True}, got
    rz, ry = await _msg_row(engine, z), await _msg_row(engine, y)
    assert rz is not None and ry is not None
    assert (rz["recipient_id"], rz["is_adapted"], rz["has_adapted"]) == (igor.uid, True, True)
    assert ry["status"] == "answered"


async def test_r2_audit_written_before_delete_is_anonymized(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Обратный порядок к A1 `test_audit_insert_waits_for_running_delete`: запись журнала
    старой сессии начата раньше удаления (держит FOR KEY SHARE строки users из политики) —
    удаление ждёт её фиксации и обезличивает и её (ТЗ §9.1 п.5, АР-23)."""
    anna = await _onboard(sessionmaker, keys, "Анна", "Игорь")
    mark = uuid.uuid4()  # object_id-метка записи (не id пользователя)
    async with user_scope(sessionmaker, anna.uid) as s:
        await s.execute(
            text(
                "INSERT INTO audit_log (actor_id, action, object_type, object_id) "
                "VALUES (app_uid(), 'me.export', 'export', :o)"
            ),
            {"o": mark},
        )
        t_del = asyncio.ensure_future(_outcome(sessionmaker, anna.uid, DELETE))
        await _wait_lock_waiters(engine, "app_delete_account", 1)
    assert await asyncio.wait_for(t_del, timeout=30) is None
    rows = await _su_rows(
        engine, "SELECT actor_id, action FROM audit_log WHERE object_id = :o", o=mark
    )
    assert [tuple(r) for r in rows] == [(None, "me.export")]
    assert await _su(engine, "SELECT count(*) FROM audit_log WHERE actor_id = :u", u=anna.uid) == 0


async def test_r2_audit_insert_rules_for_living_and_foreign(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Политика audit_insert (0005): живой — со своим id и с NULL; чужой id — 42501 (в т.ч. id
    уже удалённого); без входа — запись без id; удалённый — только NULL (без id)."""
    anna = await _onboard(sessionmaker, keys, "Анна", "Игорь")
    igor = await _onboard(sessionmaker, keys, "Игорь", "Аня")
    null_row = (
        "INSERT INTO audit_log (actor_id, action, object_type) "
        "VALUES (NULL, 'me.view', 'user') RETURNING 1"
    )
    foreign = (
        "INSERT INTO audit_log (actor_id, action, object_type) "
        "VALUES (:a, 'me.view', 'user') RETURNING 1"
    )
    assert isinstance(await _outcome(sessionmaker, igor.uid, AUDIT_SELF), int)
    assert await _outcome(sessionmaker, igor.uid, null_row) == 1
    assert await _outcome(sessionmaker, igor.uid, foreign, a=anna.uid) == "42501"
    # Без входа app_uid() — NULL: запись проходит обезличенной (actor_id NULL), id нет.
    assert await _outcome(sessionmaker, None, AUDIT_SELF) == 1
    assert await _outcome(sessionmaker, None, null_row) == 1
    await _scalar(sessionmaker, anna.uid, DELETE)
    assert await _outcome(sessionmaker, igor.uid, foreign, a=anna.uid) == "42501"
    assert await _outcome(sessionmaker, anna.uid, AUDIT_SELF) == "42501"
    assert await _outcome(sessionmaker, anna.uid, null_row) == 1
    assert await _su(engine, "SELECT count(*) FROM audit_log WHERE actor_id = :u", u=anna.uid) == 0


async def test_r2_free_running_delete_leave_send_pipeline(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Без принудительного порядка, 15 прогонов: Анна удаляет аккаунт; Игорь одновременно
    выходит из пары и отправляет черновик; конвейер доставляет его ответ на сообщение Анны и
    останавливает другое его сообщение ей. Допустимо: удаление — успех; выход — id пары или
    FB007 (пару уже распустило удаление); отправка — id или FA003; доставка и остановка —
    True. Ни 40P01, ни 23503; после — у Игоря только свои сообщения без получателя и без
    версии для получателя."""
    for _ in range(15):
        anna, igor, couple, draft = await _ready_pair(sessionmaker, keys)
        _, x = await _send(sessionmaker, keys, anna, f"X {anna.marker}")
        await _deliver(engine, keys, x, f"X* {anna.marker}")
        _, y = await _send(sessionmaker, keys, igor, f"Y {igor.marker}", x)
        _, w = await _send(sessionmaker, keys, igor, f"W {igor.marker}")
        res = await asyncio.wait_for(
            asyncio.gather(
                _outcome(sessionmaker, anna.uid, DELETE),
                _outcome(sessionmaker, igor.uid, LEAVE),
                _outcome(sessionmaker, igor.uid, "SELECT app_send_draft(:d, 1)", d=draft),
                _deliver_adapted(engine, keys, y, f"Y* {igor.marker}"),
                _withhold_outcome(engine, w),
            ),
            timeout=30,
        )
        deleted, left, sent, delivered, withheld = res
        assert not _BAD & {str(v) for v in res}, res
        assert deleted is None, res
        assert left == couple or left == "FB007", res
        assert isinstance(sent, uuid.UUID) or sent == "FA003", res
        assert (delivered, withheld) == (True, True), res
        rows = await _su_rows(
            engine,
            "SELECT author_id, recipient_id, reply_to_id, adapted_enc, is_adapted "
            "FROM partner_messages WHERE couple_id = :c",
            c=couple,
        )
        assert rows and all(
            r.author_id == igor.uid
            and r.recipient_id is None
            and r.reply_to_id is None
            and r.adapted_enc is None
            and r.is_adapted is False
            for r in rows
        ), rows


async def test_r2_free_running_both_partners_delete_with_pipeline(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """15 прогонов без порядка: партнёры активной пары (чётные прогоны) или бывшие (нечётные)
    удаляют аккаунты одновременно, пока конвейер доставляет встречные ответы Y и Z. Удаления —
    успех, доставки — True/False (сообщение уже удалено); ни 40P01, ни 23503; после — ни
    пользователей, ни пары, ни сообщений."""
    for n in range(15):
        anna = await _onboard(sessionmaker, keys, "Анна", "Игорь")
        igor = await _onboard(sessionmaker, keys, "Игорь", "Аня")
        couple = await _pair(sessionmaker, anna, igor)
        _, x = await _send(sessionmaker, keys, anna, f"X {anna.marker}")
        await _deliver(engine, keys, x, "X*")
        _, y = await _send(sessionmaker, keys, igor, f"Y {igor.marker}", x)
        await _deliver(engine, keys, y, "Y*")
        _, z = await _send(sessionmaker, keys, anna, f"Z {anna.marker}", y)
        _, y2 = await _send(sessionmaker, keys, igor, f"Y2 {igor.marker}")
        if n % 2:
            await couples.leave(sessionmaker, igor.uid)
        res = await asyncio.wait_for(
            asyncio.gather(
                _outcome(sessionmaker, anna.uid, DELETE),
                _outcome(sessionmaker, igor.uid, DELETE),
                _deliver_adapted(engine, keys, z, "Z*"),
                _deliver_adapted(engine, keys, y2, "Y2*"),
            ),
            timeout=30,
        )
        assert not _BAD & {str(v) for v in res}, res
        assert list(res[:2]) == [None, None], res
        assert all(v in (True, False) for v in res[2:]), res
        for sql in (
            "SELECT count(*) FROM couples WHERE id = :c",
            "SELECT count(*) FROM partner_messages WHERE couple_id = :c",
        ):
            assert await _su(engine, sql, c=couple) == 0, (sql, res)


async def test_r2_free_running_delete_vs_accept_invite(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """15 прогонов без порядка: автор ссылки удаляет аккаунт, двое гостей принимают её.
    Удаление — успех; гости — id пары (не более одного), FB001 (ссылки уже нет), FB003
    (принята другим или отозвана) или FB006; ни 40P01, ни 23503. После — гости свободны."""
    for _ in range(15):
        vera = await _onboard(sessionmaker, keys, "Вера", "Пётр")
        g1 = await _onboard(sessionmaker, keys, "Гость1", "Вера")
        g2 = await _onboard(sessionmaker, keys, "Гость2", "Вера")
        tok = _token()
        issued = await couples.create_with_invite(sessionmaker, vera.uid, tok)
        accept = "SELECT app_accept_invite(:h)"
        res = await asyncio.wait_for(
            asyncio.gather(
                _outcome(sessionmaker, vera.uid, DELETE),
                _outcome(sessionmaker, g1.uid, accept, h=tok),
                _outcome(sessionmaker, g2.uid, accept, h=tok),
            ),
            timeout=30,
        )
        assert not _BAD & {str(v) for v in res}, res
        assert res[0] is None, res
        assert all(v == issued.couple_id or v in ("FB001", "FB003", "FB006") for v in res[1:]), res
        assert sum(1 for v in res[1:] if v == issued.couple_id) <= 1, res
        for g in (g1, g2):
            assert await couples.get_current(sessionmaker, g.uid) is None
        assert await _su(engine, "SELECT count(*) FROM invites WHERE token_hash = :h", h=tok) == 0


@pytest.mark.xfail(
    reason="MINOR M-2 (docs/reviews/4.5-test.md, раунд 2): политика audit_insert 0005 берёт "
    "FOR KEY SHARE строки users; две транзакции одного пользователя «аудит → app_delete_account» "
    "повышают блокировку до FOR UPDATE навстречу друг другу: 40P01 вместо 42501",
    strict=True,
)
async def test_r2_two_requests_audit_then_delete_same_user_no_deadlock(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring
) -> None:
    """Два одновременных запроса одного пользователя (двойное нажатие «Удалить», этап 2):
    каждый в своей транзакции пишет аудит со своим actor_id, затем вызывает
    app_delete_account. Ожидание: одно удаление проходит, второе — 42501 (пользователя уже
    нет), без 40P01. Политика audit_insert (0005) берёт FOR KEY SHARE строки users, удаление —
    FOR UPDATE той же строки: повышение блокировки в двух транзакциях."""
    anna = await _onboard(sessionmaker, keys, "Анна", "Игорь")
    s1_ready, s2_ready = asyncio.Event(), asyncio.Event()

    async def request(mine: asyncio.Event, other: asyncio.Event) -> object:
        try:
            async with user_scope(sessionmaker, anna.uid) as s:
                await s.execute(text(AUDIT_SELF))
                mine.set()
                await other.wait()
                await s.execute(text(DELETE))
            return None
        except DBAPIError as exc:
            return _code(exc)

    res = await asyncio.wait_for(
        asyncio.gather(request(s1_ready, s2_ready), request(s2_ready, s1_ready)), timeout=30
    )
    assert sorted(map(str, res)) == ["42501", "None"], res
    assert await _su(engine, "SELECT count(*) FROM users WHERE id = :u", u=anna.uid) == 0


@pytest.mark.parametrize("first", ["delete", "leave"])
async def test_r2_race_delete_vs_partner_leave_ordered(
    sessionmaker: SM, engine: AsyncEngine, keys: Keyring, first: str
) -> None:
    """Анна удаляет аккаунт, Игорь одновременно выходит из пары (ТЗ §4.5 п.2–3, АР-13).
    Держатель пары задаёт порядок. Без 40P01: первый распускает пару, второй получает FB007
    (у удаления он перехвачен — удаление успешно). Игорь свободен, его сообщение — у него."""
    anna, igor, couple, _ = await _ready_pair(sessionmaker, keys)
    _, m = await _send(sessionmaker, keys, igor, f"до выхода {igor.marker}")
    delete = ("app_delete_account", _outcome(sessionmaker, anna.uid, DELETE))
    leave = ("app_leave_couple", _outcome(sessionmaker, igor.uid, LEAVE))
    steps = [delete, leave] if first == "delete" else [leave, delete]
    res = await _hold_race(
        engine, "SELECT 1 FROM couples WHERE id = :c FOR UPDATE", {"c": couple}, steps
    )
    got = dict(zip([s[0] for s in steps], res, strict=True))
    assert got == {
        "app_delete_account": None,
        "app_leave_couple": "FB007" if first == "delete" else couple,
    }, got
    assert await couples.get_current(sessionmaker, igor.uid) is None
    row = await _msg_row(engine, m)
    assert row is not None and (row["author_id"], row["recipient_id"]) == (igor.uid, None)
    assert await _su(engine, "SELECT count(*) FROM users WHERE id = :u", u=anna.uid) == 0
