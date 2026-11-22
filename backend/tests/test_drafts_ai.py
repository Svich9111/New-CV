"""3.1, часть A3: черновик сообщения партнёру по разговору (ФТ-07; ТЗ §4.3, §6.1, §6.2 п.1–2,
§9.3; АР-20, АР-27; Б-77). Модель — заглушка; данные выдуманные."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai import drafts
from app.ai.drafts import DraftCrisis, DraftError, DraftResult
from app.ai.knowledge import KnowledgeFragment
from app.ai.llm import LLMRefusal, LLMUnavailable, StructuredResult
from app.ai.models import AgentModels
from app.ai.stub import ScriptedProvider
from app.core import crypto
from app.repositories import chat, texts
from tests.test_chat_context import KEYS, P, person, world
from tests.test_couple_functions import SM

FRAGMENT = (
    "Когда один партнёр тратит деньги без обсуждения, другой чувствует тревогу и обиду, и "
    "разговор лучше начинать с общей цели пары."
)


def draft_answer(text_: str) -> dict[str, Any]:
    return {
        "feeling": "Мне тревожно",
        "situation": "крупные траты без обсуждения",
        "need": "предсказуемость",
        "request": "обсуждать траты больше 5000 заранее",
        "text": text_,
    }


class Model(ScriptedProvider):
    """``drafts`` — тексты по очереди (последний повторяется); ``safety`` — категория;
    ``fail`` — исключение на вызове черновика."""

    def __init__(self, *drafts_: str, safety: str = "none", fail: Exception | None = None) -> None:
        super().__init__(self._handle)
        self.drafts = list(drafts_) or [
            "Мне тревожно, когда крупные траты не обсуждаются. Давай обсуждать траты заранее?"
        ]
        self.safety = safety
        self.fail = fail
        self.calls: dict[str, list[dict[str, Any]]] = {}

    def _handle(self, role: str, payload: dict[str, Any], system: str) -> dict[str, Any]:
        self.calls.setdefault(role, []).append(payload)
        if role == "safety":
            return {"category": self.safety, "secondary": []}
        assert role == "draft", role
        t = self.drafts.pop(0) if len(self.drafts) > 1 else self.drafts[0]
        return draft_answer(t)

    async def complete_json(self, **kw: Any) -> StructuredResult:
        if self.fail is not None and "need" in kw["schema"]["properties"]:
            raise self.fail
        return await super().complete_json(**kw)


async def converse(sm: SM, p: P, *pairs: tuple[str, str]) -> None:
    for owner, agent in pairs:
        await chat.add_message(sm, KEYS, p.user_id, p.conv, "owner", owner)
        await chat.add_message(sm, KEYS, p.user_id, p.conv, "agent", agent)


async def draft_rows(engine: AsyncEngine, p: P) -> list[Any]:
    async with engine.connect() as c:
        return list(
            await c.execute(
                text(
                    "SELECT id, text_enc, version, status::text AS status, reply_to_id FROM message_drafts WHERE agent_id = :a ORDER BY created_at"
                ),
                {"a": p.agent_id},
            )
        )


async def partner_messages(engine: AsyncEngine, p: P) -> int:
    async with engine.connect() as c:
        return int(
            (
                await c.execute(
                    text("SELECT count(*) FROM partner_messages WHERE author_id = :u"),
                    {"u": p.user_id},
                )
            ).scalar_one()
        )


class KB:
    async def search(
        self, query: str, *, vector: Sequence[float] | None, limit: int = 5
    ) -> list[KnowledgeFragment]:
        return [KnowledgeFragment("kb-1", FRAGMENT)]


# --------------------------------------------------------------------------- генерация


async def test_generate_draft_owner_only_context_and_encrypted(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    w = await world(engine, sessionmaker)
    before = await partner_messages(engine, w.anna)
    await converse(
        sessionmaker,
        w.anna,
        ("Игорь опять купил дорогую вещь без разговора", "Слышу тревогу. Предложить текст?"),
    )
    m = Model()
    out = await drafts.generate_draft(sessionmaker, KEYS, w.anna.user_id, AgentModels.single(m))
    assert isinstance(out, DraftResult) and out.version == 1 and out.reply_to_id is None
    assert (out.feeling, out.request) == ("Мне тревожно", "обсуждать траты больше 5000 заранее")
    [row] = [r for r in await draft_rows(engine, w.anna) if r.id == out.draft_id]
    assert row.status == "draft" and out.text.encode() not in row.text_enc
    assert KEYS.open(crypto.DRAFT_TEXT, out.draft_id, row.text_enc) == out.text
    [payload] = m.calls["draft"]
    sent = json.dumps(m.requests, ensure_ascii=False)
    for other in (w.igor, w.boris, w.olga):
        assert other.label not in sent
    for hidden in (
        "igor-processing",
        "igor-withheld",
        "anna-withheld",
        "igor-delivered",
        "ex-to-anna",
    ):
        assert w.texts[hidden] not in sent, hidden
    assert w.texts["igor-delivered-adapted"] in sent  # доставленное — как видела Анна
    assert payload["dialog"][-1] == {
        "from": "owner",
        "text": "Игорь опять купил дорогую вещь без разговора",
    }
    assert "knowledge" not in payload and payload["revision"] is None
    assert await partner_messages(engine, w.anna) == before  # ничего не отправлено (ФТ-08)


async def test_crisis_no_draft_protocol(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await converse(sessionmaker, p, ("Он толкнул меня, мне страшно", "Мне очень жаль."))
    m = Model(safety="violence")
    out = await drafts.generate_draft(sessionmaker, KEYS, p.user_id, AgentModels.single(m))
    assert isinstance(out, DraftCrisis) and out.safety_events == ("violence",)
    assert "насилие" in out.response.text
    assert "draft" not in m.calls and await draft_rows(engine, p) == []


async def test_quote_retried_then_error(sessionmaker: SM, engine: AsyncEngine) -> None:
    """Б-77: цитата из базы знаний в черновике — повтор с пожеланием, затем ошибка без записи."""
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await converse(sessionmaker, p, ("Траты без обсуждения", "Понимаю."))
    m = Model(
        "Олег, " + FRAGMENT,
        "Олег, мне тревожно, когда траты не обсуждаются. Давай обсуждать заранее?",
    )
    out = await drafts.generate_draft(
        sessionmaker, KEYS, p.user_id, AgentModels.single(m), knowledge=KB()
    )
    assert isinstance(out, DraftResult) and "без обсуждения, другой" not in out.text
    assert (
        len(m.calls["draft"]) == 2
        and m.calls["draft"][1]["revision"]["wish"] == drafts.AVOID_QUOTE_WISH
    )
    always = Model("Олег, " + FRAGMENT)
    with pytest.raises(DraftError) as ei:
        await drafts.generate_draft(
            sessionmaker, KEYS, p.user_id, AgentModels.single(always), knowledge=KB()
        )
    assert ei.value.code == "quote"
    assert len(await draft_rows(engine, p)) == 1  # только первый, удачный


@pytest.mark.parametrize("exc", [LLMUnavailable("503"), LLMRefusal("отказ")])
async def test_model_errors_typed_and_nothing_written(
    sessionmaker: SM, engine: AsyncEngine, exc: Exception
) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await converse(sessionmaker, p, ("Хочу написать про траты", "Хорошо."))
    with pytest.raises(type(exc)) as ei:
        await drafts.generate_draft(
            sessionmaker, KEYS, p.user_id, AgentModels.single(Model(fail=exc))
        )
    assert ei.value.user_message  # понятный текст для человека (АР-20)
    assert await draft_rows(engine, p) == []
    _, turns = await chat.read_history(sessionmaker, KEYS, p.user_id, p.conv)
    assert [t.text for t in turns] == ["Хочу написать про траты", "Хорошо."]  # разговор цел


async def test_no_owner_turns(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    with pytest.raises(DraftError) as ei:
        await drafts.generate_draft(sessionmaker, KEYS, p.user_id, AgentModels.single(Model()))
    assert ei.value.code == "no_turns"


# --------------------------------------------------------------------------- ответ на сообщение


async def test_reply_to_only_delivered_incoming_of_current_couple(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    w = await world(engine, sessionmaker)
    await converse(sessionmaker, w.anna, ("Хочу ответить Игорю", "Давай."))
    async with engine.connect() as c:
        ids = {
            r.label: r.id
            for r in await c.execute(
                text(
                    "SELECT pm.id, CASE WHEN pm.author_id = :a THEN 'own' WHEN pm.status::text IN "
                    "('processing', 'withheld') THEN 'pending' WHEN pm.couple_id IN (SELECT couple_id "
                    "FROM couple_members WHERE user_id = :b) THEN 'ex' ELSE 'ok' END AS label "
                    "FROM partner_messages pm WHERE pm.author_id = :a OR pm.recipient_id = :a"
                ),
                {"a": w.anna.user_id, "b": w.boris.user_id},
            )
        }
    m = Model()
    out = await drafts.generate_draft(
        sessionmaker, KEYS, w.anna.user_id, AgentModels.single(m), reply_to_id=ids["ok"]
    )
    assert isinstance(out, DraftResult) and out.reply_to_id == ids["ok"]
    for bad in ("own", "ex", uuid.uuid4()):
        target = ids[bad] if isinstance(bad, str) else bad
        with pytest.raises(DraftError) as ei:
            await drafts.generate_draft(
                sessionmaker, KEYS, w.anna.user_id, AgentModels.single(m), reply_to_id=target
            )
        assert ei.value.code == "bad_reply_target"


# --------------------------------------------------------------------------- другой вариант


async def test_regenerate_uses_previous_and_rejects_same(
    sessionmaker: SM, engine: AsyncEngine
) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await converse(sessionmaker, p, ("Траты без обсуждения", "Понимаю."))
    first = await drafts.generate_draft(
        sessionmaker, KEYS, p.user_id, AgentModels.single(Model("Вариант один, про траты."))
    )
    assert isinstance(first, DraftResult)
    m = Model("ВАРИАНТ ОДИН — про траты!", "Вариант два: давай обсуждать траты заранее?")
    second = await drafts.regenerate_draft(
        sessionmaker, KEYS, p.user_id, AgentModels.single(m), first.draft_id, wish="короче"
    )
    assert (
        isinstance(second, DraftResult)
        and second.version == 2
        and second.draft_id == first.draft_id
    )
    assert second.text == "Вариант два: давай обсуждать траты заранее?"
    calls = m.calls["draft"]
    assert calls[0]["revision"] == {"previous": "Вариант один, про траты.", "wish": "короче"}
    assert calls[1]["revision"]["wish"] == drafts.ANOTHER_WISH  # совпал с прежним — ещё раз
    stuck = Model("Вариант два: давай обсуждать траты заранее?")
    with pytest.raises(DraftError) as ei:
        await drafts.regenerate_draft(
            sessionmaker, KEYS, p.user_id, AgentModels.single(stuck), first.draft_id
        )
    assert ei.value.code == "same"
    got = await texts.get_draft(sessionmaker, KEYS, p.user_id, first.draft_id)
    assert got is not None and (got.version, got.text) == (2, second.text)


async def test_regenerate_only_own_draft_status(
    sessionmaker: SM, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    b = await person(engine, sessionmaker, "Олег", "Вера", chat_turns=False)
    await converse(sessionmaker, a, ("Траты", "Да."))
    first = await drafts.generate_draft(
        sessionmaker, KEYS, a.user_id, AgentModels.single(Model("Вариант один."))
    )
    assert isinstance(first, DraftResult)
    with pytest.raises(DraftError) as ei:
        await drafts.regenerate_draft(
            sessionmaker, KEYS, b.user_id, AgentModels.single(Model("x")), first.draft_id
        )
    assert ei.value.code == "not_found"
    # Автор поправил текст, пока шла модель, — новая версия не затирает правку.
    real = texts.get_draft

    async def edited_meanwhile(*args: Any) -> Any:
        d = await real(*args)
        await texts.update_draft_text(sessionmaker, KEYS, a.user_id, first.draft_id, "моя правка")
        return d

    monkeypatch.setattr(texts, "get_draft", edited_meanwhile)
    with pytest.raises(DraftError) as ei:
        await drafts.regenerate_draft(
            sessionmaker, KEYS, a.user_id, AgentModels.single(Model("Вариант два.")), first.draft_id
        )
    assert ei.value.code == "not_editable"
    monkeypatch.setattr(texts, "get_draft", real)
    got = await texts.get_draft(sessionmaker, KEYS, a.user_id, first.draft_id)
    assert got is not None and got.text == "моя правка"
    async with engine.begin() as c:
        await c.execute(
            text("UPDATE message_drafts SET status = 'rejected' WHERE id = :d"),
            {"d": first.draft_id},
        )
    with pytest.raises(DraftError) as ei:
        await drafts.regenerate_draft(
            sessionmaker, KEYS, a.user_id, AgentModels.single(Model("y")), first.draft_id
        )
    assert ei.value.code == "not_editable"


async def test_regenerate_crisis_keeps_draft(sessionmaker: SM, engine: AsyncEngine) -> None:
    p = await person(engine, sessionmaker, "Вера", "Олег", chat_turns=False)
    await converse(sessionmaker, p, ("Траты", "Да."))
    first = await drafts.generate_draft(
        sessionmaker, KEYS, p.user_id, AgentModels.single(Model("Вариант один."))
    )
    assert isinstance(first, DraftResult)
    await converse(sessionmaker, p, ("Мне не хочется жить", "Я рядом."))
    out = await drafts.regenerate_draft(
        sessionmaker,
        KEYS,
        p.user_id,
        AgentModels.single(Model("z", safety="self_harm")),
        first.draft_id,
    )
    assert isinstance(out, DraftCrisis) and out.safety_events == ("self_harm",)
    got = await texts.get_draft(sessionmaker, KEYS, p.user_id, first.draft_id)
    assert got is not None and got.version == 1


def test_send_only_via_app_send_draft_guard() -> None:
    """Функции черновика не вызывают отправку: путь к партнёру — только ``app_send_draft``."""
    import inspect

    src = inspect.getsource(drafts)
    assert "texts.send_draft" not in src and "SELECT app_send_draft" not in src
