"""Черновик сообщения партнёру по текущему разговору (ФТ-07; ТЗ §4.3, §6.2 п.1–2, §2 п.1–3, §9.3;
задача 3.1, часть A3).

Для API (A2, ``/drafts*``) — две функции:

- ``generate_draft`` — контекст 2.1 (``build_context``: только данные владельца и доставленные
  сообщения его пары; разговор — до последней реплики владельца) → вторая линия безопасности
  (``chat.screen``: при кризисе черновика нет, ответ — протокол §9.3) → ``dialog.draft``
  (``draft.v0``, схема «чувство — ситуация — потребность — просьба»; фрагменты базы знаний во вход
  черновика не идут) → проверка цитирования базы знаний (Б-77: ``quotes``; цитата — повтор, затем
  ошибка) → запись ``texts.create_draft`` (шифрование, AAD черновика). Ничего не отправляется:
  партнёру — только ``app_send_draft`` по «Отправить» (ФТ-08, §2 п.2).
- ``regenerate_draft`` — «другой вариант»: прежний текст и пожелание идут модели как
  ``revision``; вариант, совпавший с прежним после нормализации, — повтор, затем ошибка; новая
  версия — только в статусе ``draft`` и при неизменной версии (автор не правил, пока шла модель).

Ошибки модели — подклассы ``LLMError`` (у каждой ``user_message``; отказ на резерв не уходит —
АР-20), черновик при этом не пишется, разговор цел. Прочие отказы — ``DraftError(code)``.
Тексты в журналы не пишутся."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import chat, crisis
from app.ai.calls import CallLog, CallRecord
from app.ai.context import BuiltContext, build_context
from app.ai.dialog import Draft, Revision, draft
from app.ai.embeddings import Embedder
from app.ai.knowledge import KnowledgeBase
from app.ai.models import AgentModels
from app.ai.prompts import V0, PromptSet
from app.ai.quotes import QuoteIndex, find_quote
from app.core.crypto import Keyring
from app.core.fingerprint import normalize
from app.repositories import chat as chat_repo
from app.repositories import texts

RETRIES = 1  # повтор генерации при цитате или совпадении с прежним вариантом
AVOID_QUOTE_WISH = (
    "Сформулируй своими словами владельца, без фраз из книг и статей, коротко и по схеме."
)
ANOTHER_WISH = "Другой вариант: сформулируй иначе, другими словами, сохранив смысл и просьбу."
# 3.1 р1 (A2), ревью MINOR-3: текст черновика — не длиннее, чем принимает правка автором
# (`PATCH /drafts/{id}`, 4000). Длиннее — ещё одна попытка с пожеланием «короче», затем обрезка
# по границе предложения (автор видит и правит черновик до «Отправить»; кода ошибки нет).
MAX_DRAFT_CHARS = 4000
SHORTER_WISH = "Короче: сохрани чувство, потребность и просьбу, убери лишнее."
_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")

DraftErrorCode = Literal[
    "no_turns",  # в разговоре нет реплик владельца — писать не по чему
    "not_found",  # черновика нет среди своих
    "not_editable",  # черновик не в статусе draft или его изменили, пока шла модель
    "bad_reply_target",  # reply_to_id — не доставленное владельцу сообщение текущей пары
    "quote",  # модель повторно вставила цитату из базы знаний (Б-77)
    "same",  # «другой вариант» совпал с прежним
    "owner_gone",  # владелец удалён / нет агента
]


class DraftError(Exception):
    def __init__(self, code: DraftErrorCode) -> None:
        super().__init__(code)
        self.code: DraftErrorCode = code


@dataclass(frozen=True)
class DraftResult:
    draft_id: uuid.UUID
    version: int
    text: str
    reply_to_id: uuid.UUID | None
    # Части схемы §4.3 — для отладки и экрана (в контракте ``Draft`` только ``text``).
    feeling: str
    situation: str
    need: str
    request: str
    prompts: tuple[str, ...]
    calls: tuple[CallRecord, ...] = field(default_factory=tuple)
    kind: Literal["draft"] = "draft"


@dataclass(frozen=True)
class DraftCrisis:
    """Кризис (§9.3): черновика нет; ``response.text`` — ответ владельцу по протоколу,
    ``safety_events`` — типы для ``chat_repo.record_safety_events`` (без текста)."""

    response: crisis.CrisisResponse
    safety_events: tuple[str, ...]
    calls: tuple[CallRecord, ...] = field(default_factory=tuple)
    kind: Literal["crisis"] = "crisis"


async def _context(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    *,
    embedder: Embedder | None,
    knowledge: KnowledgeBase | None,
    prompt_set: PromptSet,
) -> BuiltContext:
    try:
        conv = await chat_repo.current_conversation(sm, user_id)
        built = await build_context(
            sm, keys, user_id, conv, query="", knowledge=None, prompt_set=prompt_set, for_draft=True
        )
        if embedder is not None or knowledge is not None:
            query = next(t.text for t in reversed(built.dialog) if t.author == "owner")
            built = await build_context(
                sm, keys, user_id, conv, query=query, embedder=embedder, knowledge=knowledge,
                prompt_set=prompt_set, for_draft=True,
            )  # fmt: skip
    except chat_repo.OwnerGone:
        raise DraftError("owner_gone") from None
    except ValueError:
        raise DraftError("no_turns") from None
    return built


async def _screen(
    models: AgentModels, built: BuiltContext, prompt_set: PromptSet, log: CallLog
) -> DraftCrisis | None:
    sc = await chat.screen(models, built, prompt_set=prompt_set)
    log.records.extend(sc.calls)
    if not sc.is_crisis:
        return None
    response = crisis.respond(sc.crisis_types, prompt_set=prompt_set, withheld=False)
    return DraftCrisis(response, response.types, tuple(log.records))


def _has_quote(d: Draft, built: BuiltContext) -> bool:
    index = QuoteIndex(built.context.knowledge)
    return any(find_quote(t, index) is not None for t in (d.text, d.request, d.need, d.situation))


async def _generate(
    models: AgentModels,
    built: BuiltContext,
    *,
    previous: str | None,
    wish: str | None,
    prompt_set: PromptSet,
    log: CallLog,
) -> Draft:
    """Черновик моделью; цитата базы знаний или повтор прежнего варианта — ещё одна попытка с
    пожеланием, затем ``DraftError``."""
    revision = Revision(previous, wish or ANOTHER_WISH) if previous is not None else None
    for attempt in range(RETRIES + 1):
        result = await draft(
            models, built.context, built.dialog, revision=revision, prompt_set=prompt_set, log=log
        )
        quoted = _has_quote(result, built)
        same = previous is not None and normalize(result.text) == normalize(previous)
        too_long = len(result.text) > MAX_DRAFT_CHARS
        if not quoted and not same and not too_long:
            return result
        if attempt == RETRIES:
            if quoted or same:
                raise DraftError("quote" if quoted else "same")
            return replace(result, text=clip_draft(result.text))
        wish_ = AVOID_QUOTE_WISH if quoted else ANOTHER_WISH if same else SHORTER_WISH
        revision = Revision(result.text, wish_)
    raise AssertionError("недостижимо")  # pragma: no cover


def clip_draft(body: str, limit: int = MAX_DRAFT_CHARS) -> str:
    """3.1 р1 (A2): текст не длиннее ``limit`` — по последней границе предложения в пределе
    (если она есть во второй половине), иначе — по последнему пробелу; пробелы по краям убраны."""
    if len(body) <= limit:
        return body
    head = body[:limit]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    cut = ends[-1] if ends and ends[-1] >= limit // 2 else head.rfind(" ")
    return (head[:cut] if cut > 0 else head).strip()


def _result(
    d: Draft, draft_id: uuid.UUID, version: int, reply_to: uuid.UUID | None, log: CallLog
) -> DraftResult:
    return DraftResult(
        draft_id=draft_id,
        version=version,
        text=d.text,
        reply_to_id=reply_to,
        feeling=d.feeling,
        situation=d.situation,
        need=d.need,
        request=d.request,
        prompts=(d.prompt,),
        calls=tuple(log.records),
    )


async def generate_draft(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    models: AgentModels,
    *,
    reply_to_id: uuid.UUID | None = None,
    embedder: Embedder | None = None,
    knowledge: KnowledgeBase | None = None,
    prompt_set: PromptSet = V0,
) -> DraftResult | DraftCrisis:
    """Новый черновик по текущему разговору владельца (``POST /drafts``)."""
    if reply_to_id is not None and not await texts.reply_target_ok(sm, user_id, reply_to_id):
        raise DraftError("bad_reply_target")
    agent_id = await texts.own_agent_id(sm, user_id)
    if agent_id is None:
        raise DraftError("owner_gone")
    built = await _context(
        sm, keys, user_id, embedder=embedder, knowledge=knowledge, prompt_set=prompt_set
    )
    log = CallLog()
    found = await _screen(models, built, prompt_set, log)
    if found is not None:
        return found
    result = await _generate(
        models, built, previous=None, wish=None, prompt_set=prompt_set, log=log
    )
    draft_id = await texts.create_draft(sm, keys, user_id, agent_id, result.text, reply_to_id)
    return _result(result, draft_id, 1, reply_to_id, log)


async def regenerate_draft(
    sm: async_sessionmaker[AsyncSession],
    keys: Keyring,
    user_id: uuid.UUID,
    models: AgentModels,
    draft_id: uuid.UUID,
    *,
    wish: str | None = None,
    embedder: Embedder | None = None,
    knowledge: KnowledgeBase | None = None,
    prompt_set: PromptSet = V0,
) -> DraftResult | DraftCrisis:
    """«Другой вариант» (``POST /drafts/{id}/regenerate``): новая версия того же черновика."""
    current = await texts.get_draft(sm, keys, user_id, draft_id)
    if current is None:
        raise DraftError("not_found")
    if current.status != "draft":
        raise DraftError("not_editable")
    built = await _context(
        sm, keys, user_id, embedder=embedder, knowledge=knowledge, prompt_set=prompt_set
    )
    log = CallLog()
    found = await _screen(models, built, prompt_set, log)
    if found is not None:
        return found
    result = await _generate(
        models, built, previous=current.text, wish=wish, prompt_set=prompt_set, log=log
    )
    version = await texts.replace_draft_text(
        sm, keys, user_id, draft_id, result.text, expected_version=current.version
    )
    if version is None:
        raise DraftError("not_editable")
    return _result(result, draft_id, version, current.reply_to_id, log)
