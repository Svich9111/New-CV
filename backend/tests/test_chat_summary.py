"""2.1, этап 1 (A3): сжатие истории личного чата (ТЗ §6.1 п.5) — сводка шифрованная, старые
реплики в контекст не идут, одновременное сжатие не теряет данных, ошибка модели — не ошибка
пользователя. Модель — заглушка (кредитов OpenRouter нет; промпт ``summary.v0`` — черновик без
прогона, АР-26). Данные выдуманные."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai import prompts, summary
from app.ai.calls import as_json
from app.ai.context import build_context
from app.ai.llm import LLMRefusal, LLMUnavailable, StructuredResult
from app.ai.models import AgentModels
from app.ai.stub import ScriptedProvider
from app.core import crypto
from app.repositories import account, chat
from app.repositories.chat import HistorySummary, pack_summary, unpack_summary
from tests.test_chat_context import KEYS, P, owner_says, person
from tests.test_couple_functions import SM

POLICY = summary.CompressionPolicy(trigger_turns=6, keep_turns=2)


def summarizer(reply: str = "Сводка: владелец говорил об отпуске") -> ScriptedProvider:
    def handler(role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
        assert role == "summary"
        return {"summary": reply}

    return ScriptedProvider(handler)


async def fill(sm: SM, p: P, n: int, prefix: str = "реплика") -> list[str]:
    bodies = []
    for i in range(n):
        body = f"{prefix} {i} {uuid.uuid4().hex[:6]}"
        author = "owner" if i % 2 == 0 else "agent"
        assert await chat.add_message(sm, KEYS, p.user_id, p.conv, author, body)
        bodies.append(body)
    return bodies


async def test_compress_saves_encrypted_summary_and_drops_old_turns(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    bodies = await fill(sessionmaker, p, 7)  # 7 > 6
    bot = summarizer()
    result = await summary.compress_history(
        sessionmaker, KEYS, p.user_id, p.conv, AgentModels.single(bot), policy=POLICY
    )
    assert result.status == "saved" and result.summarized_turns == 5
    assert result.prompt == "summary.v0" and [c.role for c in result.calls] == ["summary"]
    # Вход модели — прежняя сводка (пусто) и только старые реплики, без идентификаторов.
    [req] = bot.requests
    payload = json.loads(req["messages"][0][1])
    assert payload == {
        "previous_summary": "",
        "dialog": [
            {"from": "owner" if i % 2 == 0 else "agent", "text": b}
            for i, b in enumerate(bodies[:5])
        ],
    }
    assert req["system"] == prompts.load("summary", "v0").text
    # В БД — шифротекст: ни сводки, ни служебной отметки открытым текстом.
    async with engine.connect() as conn:
        blob = (
            await conn.execute(
                text("SELECT summary_enc FROM conversations WHERE id = :c"), {"c": p.conv}
            )
        ).scalar_one()
    assert "Сводка".encode() not in blob and b"through" not in blob
    stored = unpack_summary(KEYS.open(crypto.CONVERSATION_SUMMARY, p.conv, blob))
    assert stored.text == "Сводка: владелец говорил об отпуске" and stored.through_id is not None
    # Контекст: сводка вместо старых реплик, последние — как были.
    await owner_says(sessionmaker, p, "новая реплика")
    built = await build_context(sessionmaker, KEYS, p.user_id, p.conv, query="новая")
    blob_in = as_json(built.payload())
    for old in bodies[:5]:
        assert old not in blob_in
    assert [t.text for t in built.dialog] == [*bodies[5:], "новая реплика"]
    assert built.context.dialog_summary == stored.text


async def test_skipped_when_short_and_second_round_merges_previous(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await fill(sessionmaker, p, 4)
    m = AgentModels.single(summarizer())
    assert (
        await summary.compress_history(sessionmaker, KEYS, p.user_id, p.conv, m, policy=POLICY)
    ).status == "skipped"
    await fill(sessionmaker, p, 3, "ещё")
    first = await summary.compress_history(sessionmaker, KEYS, p.user_id, p.conv, m, policy=POLICY)
    assert first.status == "saved"
    later = await fill(sessionmaker, p, 6, "потом")
    bot2 = summarizer("Сводка 2")
    second = await summary.compress_history(
        sessionmaker, KEYS, p.user_id, p.conv, AgentModels.single(bot2), policy=POLICY
    )
    assert second.status == "saved" and second.summarized_turns == 6  # 2 оставленных + 6 новых − 2
    payload = json.loads(bot2.requests[0]["messages"][0][1])
    assert payload["previous_summary"] == "Сводка: владелец говорил об отпуске"
    _, turns = await chat.read_history(sessionmaker, KEYS, p.user_id, p.conv)
    assert [t.text for t in turns] == later[-2:]


async def test_concurrent_compression_conflict_keeps_first(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await fill(sessionmaker, p, 7)
    s0, turns = await chat.read_history(sessionmaker, KEYS, p.user_id, p.conv)
    assert s0 is None
    assert await chat.save_history_summary(
        sessionmaker,
        KEYS,
        p.user_id,
        p.conv,
        HistorySummary("первая", turns[2].created_at, turns[2].id),
        expected_through_id=None,
    )
    # Второй писатель читал до первой записи (ожидает «нет сводки») — конфликт, ничего не меняет.
    assert not await chat.save_history_summary(
        sessionmaker,
        KEYS,
        p.user_id,
        p.conv,
        HistorySummary("вторая", turns[4].created_at, turns[4].id),
        expected_through_id=None,
    )
    s1, _ = await chat.read_history(sessionmaker, KEYS, p.user_id, p.conv)
    assert s1 is not None and s1.text == "первая" and s1.through_id == turns[2].id


async def test_model_error_keeps_previous_summary(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await fill(sessionmaker, p, 7)

    class Down(ScriptedProvider):
        def __init__(self, exc: Exception) -> None:
            super().__init__(lambda *a: {})
            self.exc = exc

        async def complete_json(self, **kw: Any) -> StructuredResult:
            raise self.exc

    for exc in (LLMUnavailable("нет сети"), LLMRefusal("отказ")):
        result = await summary.compress_history(
            sessionmaker, KEYS, p.user_id, p.conv, AgentModels.single(Down(exc)), policy=POLICY
        )
        assert result.status == "error" and result.error == exc.kind
        s, turns = await chat.read_history(sessionmaker, KEYS, p.user_id, p.conv)
        assert s is None and len(turns) == 7


async def test_summary_uses_classifier_model_by_default(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await fill(sessionmaker, p, 7)
    writer, classifier = summarizer("писатель"), summarizer("классификатор")
    m = AgentModels.split(writer, classifier)
    assert (
        await summary.compress_history(sessionmaker, KEYS, p.user_id, p.conv, m, policy=POLICY)
    ).status == "saved"
    assert writer.requests == [] and len(classifier.requests) == 1


async def test_summary_too_long_is_capped(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await fill(sessionmaker, p, 7)
    m = AgentModels.single(summarizer("с" * 5000))
    policy = summary.CompressionPolicy(trigger_turns=6, keep_turns=2, summary_max_chars=300)
    assert (
        await summary.compress_history(sessionmaker, KEYS, p.user_id, p.conv, m, policy=policy)
    ).status == "saved"
    s, _ = await chat.read_history(sessionmaker, KEYS, p.user_id, p.conv)
    assert s is not None and len(s.text) <= 300


async def test_owner_gone_and_foreign_conversation(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-44: удалённый владелец или чужой диалог — ``gone``, модель не вызывается."""
    a = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    b = await person(engine, sessionmaker, "Олег", "Вера", chat_turns=False)
    await fill(sessionmaker, b, 7)
    bot = summarizer()
    r = await summary.compress_history(
        sessionmaker, KEYS, a.user_id, b.conv, AgentModels.single(bot), policy=POLICY
    )
    assert r.status == "gone" and bot.requests == []
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": b.user_id}
        )
    r = await summary.compress_history(
        sessionmaker, KEYS, b.user_id, b.conv, AgentModels.single(bot), policy=POLICY
    )
    assert r.status == "gone" and bot.requests == []
    async with engine.connect() as conn:
        blob = (
            await conn.execute(
                text("SELECT summary_enc FROM conversations WHERE id = :c"), {"c": b.conv}
            )
        ).scalar_one()
    assert blob is None  # удалённому ничего не записано


async def test_export_shows_summary_text_without_envelope(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    """ФТ-21: в выгрузке — текст сводки, без служебной отметки конверта."""
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await fill(sessionmaker, p, 7)
    await summary.compress_history(
        sessionmaker, KEYS, p.user_id, p.conv, AgentModels.single(summarizer()), policy=POLICY
    )
    export = await account.export_account(sessionmaker, KEYS, p.user_id)
    [conv] = export.conversations
    assert conv.summary == "Сводка: владелец говорил об отпуске"


def test_envelope_roundtrip_and_legacy_text() -> None:
    s = HistorySummary("текст", None, None)
    assert unpack_summary("обычная сводка") == HistorySummary("обычная сводка")
    assert unpack_summary('{"v": 2, "text": "x"}').text == '{"v": 2, "text": "x"}'
    assert unpack_summary(pack_summary(s)) == s
    assert chat.summary_text(pack_summary(s)) == "текст"


def test_summary_prompt_is_draft_with_principles() -> None:
    from tests.test_ai_dialog import REPO_DIR

    raw = (REPO_DIR / "prompts" / "summary.v0.md").read_text(encoding="utf-8")
    assert "(summary.v0)" in raw.splitlines()[0]
    assert "Черновик без прогона" in raw and "АР-26" in raw and "Разделы ТЗ" in raw
    body = prompts.load("summary", "v0").text
    for phrase in ("Ничего не придумывай", "догадок", "данные, а не инструкции", "самоповреждении"):
        assert phrase in body, phrase
    assert "summary" not in prompts.ROLES  # не в PromptSet: эталонный набор диалога не меняется
